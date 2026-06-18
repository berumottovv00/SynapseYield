"""TradeExecutor：风控 + Broker 提交 + 止盈/止损子单执行器。

职责边界：
  - 接收已通过人工审批的 LLMTradeProposal，执行下单完整流程。
  - 不包含任何 LLM / 策略逻辑，不依赖 LLM Provider。
  - 由 HarnessAgentGraph 的 execute 节点调用。
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse_yield.domain.enums import (
    LLMTradingRunStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    TradeAction,
)
from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import (
    BrokerOrderResult,
    HumanApprovalDecision,
    LLMTradeProposal,
    LLMTradingRunResult,
    MarketQuote,
    OrderIntent,
)
from synapse_yield.storage.models import AuditLog, Order, StrategySignal
from synapse_yield.storage.session import SessionLocal

if TYPE_CHECKING:
    from synapse_yield.broker.base import BrokerAdapter
    from synapse_yield.harness.order_service import OrderService


class TradeExecutor:
    """风控 + Broker 提交的核心执行器，与 LLM 策略层完全解耦。"""

    def __init__(
        self,
        *,
        order_service: OrderService,
        broker: BrokerAdapter,
        session_factory: Callable[[], Session] = SessionLocal,
        strategy_name: str = "harness_agent",
        strategy_version: str = "1.0",
        signal_raw_extras: dict | None = None,
    ) -> None:
        self.order_service = order_service
        self.broker = broker
        self.session_factory = session_factory
        self.strategy_name = strategy_name
        self.strategy_version = strategy_version
        # 额外字段会合并进 StrategySignal.raw_payload（供 CLI 图传入 provider 信息）
        self._signal_raw_extras = signal_raw_extras or {}

    # ── 对外接口 ──────────────────────────────────────────────────────────────

    def execute(
        self,
        proposal: LLMTradeProposal,
        decision: HumanApprovalDecision,
        *,
        account_id: str,
        trace_id: str | None = None,
        thread_id: str | None = None,
        market_url: str = "",
        market_is_open: bool | None = None,
    ) -> LLMTradingRunResult:
        resolved_trace_id = trace_id or new_id("trace")
        resolved_thread_id = thread_id or new_id("thread")

        session = self.session_factory()
        try:
            result = self._do_execute(
                session,
                proposal=proposal,
                decision=decision,
                account_id=account_id,
                trace_id=resolved_trace_id,
                thread_id=resolved_thread_id,
                market_url=market_url,
                market_is_open=market_is_open,
            )
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── 内部执行逻辑 ──────────────────────────────────────────────────────────

    def _do_execute(
        self,
        session: Session,
        *,
        proposal: LLMTradeProposal,
        decision: HumanApprovalDecision,
        account_id: str,
        trace_id: str,
        thread_id: str,
        market_url: str,
        market_is_open: bool | None,
    ) -> LLMTradingRunResult:
        existing = self._find_existing_order(session, trace_id, proposal.symbol)
        if existing is not None:
            return LLMTradingRunResult(
                thread_id=thread_id,
                trace_id=trace_id,
                status=LLMTradingRunStatus.COMPLETED,
                proposal=proposal,
                reviewer=decision.reviewer,
                order_id=existing.order_id,
                risk_decision_id=existing.risk_decision_id,
                broker_order_id=existing.broker_order_id,
                order_status=existing.status,
                message="Idempotency hit: existing approved order reused",
            )

        signal_id = new_id("sig")
        session.add(self._build_signal(signal_id, trace_id, market_url, proposal, decision))
        session.flush()

        intent = OrderIntent(
            account_id=account_id,
            symbol=proposal.symbol,
            side=OrderSide(proposal.action.value),
            order_type=proposal.order_type,
            quantity=proposal.quantity,
            limit_price=proposal.limit_price,
            source_signal_id=signal_id,
            strategy_name=self.strategy_name,
        )
        order = self.order_service.create_order_from_intent(session, trace_id, intent)

        price = proposal.limit_price or Decimal("0")
        quote = MarketQuote(
            symbol=proposal.symbol,
            last_price=price,
            bid_price=price,
            ask_price=price,
        )
        risk_decision = self.order_service.run_risk_check(
            session, trace_id, order, quote=quote, market_is_open=market_is_open
        )

        if order.status != OrderStatus.RISK_APPROVED:
            raise RuntimeError(f"风控拒绝：{risk_decision.reason_code}")

        broker_result = self.broker.submit_order(session, trace_id, order, quote)
        if not broker_result.accepted:
            raise RuntimeError(f"券商拒绝下单：{broker_result.message}")

        broker_order_id = broker_result.broker_order_id

        tp_order_id = tp_broker_order_id = None
        sl_order_id = sl_broker_order_id = None
        if (
            proposal.action == TradeAction.BUY
            and (proposal.take_profit_price or proposal.stop_loss_price)
        ):
            if proposal.take_profit_price:
                tp_order, tp_r = self._submit_bracket_order(
                    session, trace_id, account_id, proposal.symbol,
                    proposal.quantity, OrderType.LIMIT_IF_TOUCHED,
                    proposal.take_profit_price, signal_id,
                )
                tp_order_id = tp_order.order_id
                tp_broker_order_id = tp_r.broker_order_id

            if proposal.stop_loss_price:
                sl_order, sl_r = self._submit_bracket_order(
                    session, trace_id, account_id, proposal.symbol,
                    proposal.quantity, OrderType.STOP_LIMIT,
                    proposal.stop_loss_price, signal_id,
                )
                sl_order_id = sl_order.order_id
                sl_broker_order_id = sl_r.broker_order_id

        self._add_audit(
            session,
            trace_id,
            "TRADE_APPROVED",
            {"proposal": proposal.model_dump(mode="json")},
            {
                "approval": decision.model_dump(mode="json"),
                "order_id": order.order_id,
                "order_status": order.status.value,
                "take_profit_order_id": tp_order_id,
                "stop_loss_order_id": sl_order_id,
            },
        )
        return LLMTradingRunResult(
            thread_id=thread_id,
            trace_id=trace_id,
            status=LLMTradingRunStatus.COMPLETED,
            proposal=proposal,
            reviewer=decision.reviewer,
            order_id=order.order_id,
            risk_decision_id=risk_decision.risk_decision_id,
            broker_order_id=broker_order_id,
            order_status=order.status,
            take_profit_order_id=tp_order_id,
            take_profit_broker_order_id=tp_broker_order_id,
            stop_loss_order_id=sl_order_id,
            stop_loss_broker_order_id=sl_broker_order_id,
            message=broker_result.message,
        )

    def _submit_bracket_order(
        self,
        session: Session,
        trace_id: str,
        account_id: str,
        symbol: str,
        quantity: Decimal,
        order_type: OrderType,
        trigger_price: Decimal,
        source_signal_id: str,
    ) -> tuple:
        intent = OrderIntent(
            account_id=account_id,
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=order_type,
            quantity=quantity,
            limit_price=trigger_price,
            trigger_price=trigger_price,
            time_in_force=TimeInForce.DAY,
            source_signal_id=source_signal_id,
            strategy_name=self.strategy_name,
        )
        order = self.order_service.create_order_from_intent(session, trace_id, intent)
        self.order_service.transition_order(
            session, trace_id, order, OrderStatus.RISK_APPROVED, "BRACKET_AUTO_APPROVED"
        )
        price = trigger_price
        quote = MarketQuote(symbol=symbol, last_price=price, bid_price=price, ask_price=price)
        broker_result = self.broker.submit_order(session, trace_id, order, quote)
        return order, broker_result

    def _build_signal(
        self,
        signal_id: str,
        trace_id: str,
        market_url: str,
        proposal: LLMTradeProposal,
        decision: HumanApprovalDecision,
    ) -> StrategySignal:
        raw_payload = {
            "market_url": market_url,
            "proposal": proposal.model_dump(mode="json"),
            "human_approval": decision.model_dump(mode="json"),
            **self._signal_raw_extras,
        }
        return StrategySignal(
            signal_id=signal_id,
            trace_id=trace_id,
            strategy_name=self.strategy_name,
            strategy_version=self.strategy_version,
            symbol=proposal.symbol,
            side=OrderSide(proposal.action.value),
            confidence=proposal.confidence,
            target_quantity=proposal.quantity,
            order_type=proposal.order_type,
            limit_price=proposal.limit_price,
            reason=proposal.rationale,
            raw_payload=raw_payload,
        )

    def _find_existing_order(
        self, session: Session, trace_id: str, symbol: str
    ) -> Order | None:
        return session.scalar(
            select(Order)
            .join(StrategySignal, Order.source_signal_id == StrategySignal.signal_id)
            .where(
                StrategySignal.trace_id == trace_id,
                StrategySignal.strategy_name == self.strategy_name,
                StrategySignal.symbol == symbol,
            )
        )

    @staticmethod
    def _add_audit(
        session: Session,
        trace_id: str,
        event_type: str,
        input_snapshot: dict,
        output_snapshot: dict,
    ) -> None:
        session.add(
            AuditLog(
                audit_id=new_id("audit"),
                trace_id=trace_id,
                actor_type="AGENT",
                actor_name="TradeExecutor",
                event_type=event_type,
                input_snapshot=input_snapshot,
                output_snapshot=output_snapshot,
            )
        )
