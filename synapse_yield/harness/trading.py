from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse_yield.broker.local_sim import LocalSimBroker
from synapse_yield.domain.enums import OrderStatus
from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import (
    HarnessRunResult,
    MarketDataSnapshot,
    MarketQuote,
    StrategyContext,
)
from synapse_yield.harness.order_service import OrderService
from synapse_yield.storage.models import Order, StrategySignal
from synapse_yield.strategy.adapters import StrategyAdapter
from synapse_yield.strategy.runner import StrategyRunner


class TradingHarness:
    """交易主编排入口，串联行情、策略、风控和执行。"""

    def __init__(
        self,
        strategy_runner: StrategyRunner | None = None,
        order_service: OrderService | None = None,
        broker: LocalSimBroker | None = None,
    ):
        self.strategy_runner = strategy_runner or StrategyRunner()
        self.order_service = order_service or OrderService()
        self.broker = broker or LocalSimBroker(order_service=self.order_service)

    def run_market_snapshot(
        self,
        session: Session,
        adapter: StrategyAdapter,
        snapshot: MarketDataSnapshot,
        *,
        account_id: str,
        trace_id: str | None = None,
        strategy_parameters: dict | None = None,
        dry_run: bool = False,
        market_is_open: bool | None = None,
    ) -> HarnessRunResult:
        """处理一条行情快照，并在非 dry run 时完成风控和 Broker 提交。"""

        current_trace_id = trace_id or new_id("trace")
        # 显式 trace_id 代表同一编排请求；重试时优先复用已经创建的订单。
        if trace_id is not None and not dry_run:
            existing_order = session.scalar(
                select(Order)
                .join(StrategySignal, Order.source_signal_id == StrategySignal.signal_id)
                .where(
                    StrategySignal.trace_id == current_trace_id,
                    StrategySignal.strategy_name == adapter.name,
                    StrategySignal.symbol == snapshot.symbol,
                )
            )
            if existing_order is not None:
                self.order_service.record_event(
                    session,
                    current_trace_id,
                    "ORDER_IDEMPOTENCY_HIT",
                    {"trace_id": current_trace_id},
                    {"order_id": existing_order.order_id},
                )
                return self._existing_order_result(
                    current_trace_id,
                    None,
                    existing_order.source_signal_id,
                    existing_order,
                )

        context = StrategyContext(
            account_id=account_id,
            parameters=strategy_parameters or {},
        )
        strategy_result = self.strategy_runner.run(
            session=session,
            trace_id=current_trace_id,
            adapter=adapter,
            snapshot=snapshot,
            context=context,
            dry_run=dry_run,
        )

        if strategy_result.output is None:
            return HarnessRunResult(
                trace_id=current_trace_id,
                snapshot_id=strategy_result.snapshot_id,
                dry_run=dry_run,
                message="Strategy produced no signal",
            )

        if dry_run:
            return HarnessRunResult(
                trace_id=current_trace_id,
                snapshot_id=strategy_result.snapshot_id,
                signal_id=strategy_result.signal_id,
                dry_run=True,
                message="Dry run completed without order creation",
            )

        if strategy_result.order_intent is None:
            return HarnessRunResult(
                trace_id=current_trace_id,
                snapshot_id=strategy_result.snapshot_id,
                signal_id=strategy_result.signal_id,
                dry_run=False,
                message="Strategy signal did not produce an order intent",
            )

        order = self.order_service.create_order_from_intent(
            session,
            current_trace_id,
            strategy_result.order_intent,
        )
        if order.status != OrderStatus.CREATED:
            return self._existing_order_result(
                current_trace_id,
                strategy_result.snapshot_id,
                strategy_result.signal_id,
                order,
            )

        quote = self._quote_from_snapshot(snapshot)
        risk_decision = self.order_service.run_risk_check(
            session,
            current_trace_id,
            order,
            quote=quote,
            market_is_open=market_is_open,
        )
        if order.status == OrderStatus.RISK_REJECTED:
            return HarnessRunResult(
                trace_id=current_trace_id,
                snapshot_id=strategy_result.snapshot_id,
                signal_id=strategy_result.signal_id,
                order_id=order.order_id,
                risk_decision_id=risk_decision.risk_decision_id,
                order_status=order.status,
                dry_run=False,
                message=f"Risk rejected order: {risk_decision.reason_code}",
            )

        broker_result = self.broker.submit_order(session, current_trace_id, order, quote)
        return HarnessRunResult(
            trace_id=current_trace_id,
            snapshot_id=strategy_result.snapshot_id,
            signal_id=strategy_result.signal_id,
            order_id=order.order_id,
            risk_decision_id=risk_decision.risk_decision_id,
            broker_order_id=broker_result.broker_order_id,
            order_status=order.status,
            dry_run=False,
            message=broker_result.message,
        )

    @staticmethod
    def _quote_from_snapshot(snapshot: MarketDataSnapshot) -> MarketQuote:
        """将策略标准行情转换为风控和 Broker 使用的报价模型。"""

        return MarketQuote(
            symbol=snapshot.symbol,
            last_price=snapshot.last_price,
            bid_price=snapshot.bid_price,
            ask_price=snapshot.ask_price,
        )

    @staticmethod
    def _existing_order_result(
        trace_id: str,
        snapshot_id: str | None,
        signal_id: str | None,
        order: Order,
    ) -> HarnessRunResult:
        """相同幂等键命中已有订单时，返回现有订单状态而不重复执行。"""

        return HarnessRunResult(
            trace_id=trace_id,
            snapshot_id=snapshot_id,
            signal_id=signal_id,
            order_id=order.order_id,
            risk_decision_id=order.risk_decision_id,
            broker_order_id=order.broker_order_id,
            order_status=order.status,
            dry_run=False,
            message="Idempotency hit: existing order reused",
        )
