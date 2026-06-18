from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse_yield.broker.longbridge.gateway import LongbridgeGateway
from synapse_yield.broker.longbridge.types import (
    BrokerAccountSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    LongbridgeSubmitRequest,
)
from synapse_yield.domain.enums import OrderStatus
from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import BrokerOrderResult, MarketDataSnapshot, MarketQuote
from synapse_yield.harness.order_service import OrderService
from synapse_yield.storage.models import BrokerEvent, Order


@dataclass(frozen=True)
class LongbridgeBrokerConfig:
    """长桥交易安全配置，默认禁止任何外部下单。"""

    mode: str = "paper"
    enable_order_submission: bool = False
    enable_live_trading: bool = False


class LongbridgeBroker:
    """通过长桥 OpenAPI 提交订单、查询账户并消费交易推送。"""

    broker_name = "longbridge"

    def __init__(
        self,
        gateway: LongbridgeGateway,
        *,
        order_service: OrderService | None = None,
        config: LongbridgeBrokerConfig | None = None,
    ):
        self.gateway = gateway
        self.order_service = order_service or OrderService()
        self.config = config or LongbridgeBrokerConfig()

    def submit_order(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        quote: MarketQuote,
    ) -> BrokerOrderResult:
        """提交订单；外部交易和 live 模式均需要显式安全开关。"""

        if order.status != OrderStatus.RISK_APPROVED:
            return BrokerOrderResult(
                accepted=False,
                message=f"Order status {order.status} is not submittable",
            )
        safety_error = self._submission_safety_error()
        if safety_error is not None:
            self.order_service.record_event(
                session,
                trace_id,
                "LONGBRIDGE_SUBMISSION_BLOCKED",
                {"order_id": order.order_id, "mode": self.config.mode},
                {"reason": safety_error},
            )
            return BrokerOrderResult(accepted=False, message=safety_error)

        self.order_service.transition_order(
            session,
            trace_id,
            order,
            OrderStatus.SUBMITTING,
            "LONGBRIDGE_ORDER_SUBMITTING",
        )
        # LIT/SLO 订单的触发价存在 raw_request 中（因 Order 模型无 trigger_price 列）
        trigger_price = None
        raw = order.raw_request or {}
        if isinstance(raw.get("trigger_price"), (int, float, str)):
            from decimal import Decimal as _D
            trigger_price = _D(str(raw["trigger_price"]))

        request = LongbridgeSubmitRequest(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.limit_price,
            time_in_force=order.time_in_force,
            trigger_price=trigger_price,
        )
        try:
            broker_order_id = self.gateway.submit_order(request)
        except (TimeoutError, ConnectionError) as exc:
            self.order_service.transition_order(
                session,
                trace_id,
                order,
                OrderStatus.RECONCILING,
                "LONGBRIDGE_SUBMIT_RESULT_UNKNOWN",
            )
            self._record_event(
                session,
                trace_id,
                order,
                "SUBMIT_UNKNOWN",
                {"error": str(exc), "client_order_id": order.client_order_id},
            )
            return BrokerOrderResult(
                accepted=False,
                message="Longbridge submit result is unknown; reconciliation required",
            )
        except Exception as exc:
            self.order_service.transition_order(
                session,
                trace_id,
                order,
                OrderStatus.FAILED,
                "LONGBRIDGE_ORDER_FAILED",
            )
            self._record_event(
                session,
                trace_id,
                order,
                "SUBMIT_FAILED",
                {"error": str(exc), "client_order_id": order.client_order_id},
            )
            return BrokerOrderResult(accepted=False, message=f"Longbridge rejected order: {exc}")

        order.broker_order_id = broker_order_id
        self.order_service.transition_order(
            session,
            trace_id,
            order,
            OrderStatus.SUBMITTED,
            "LONGBRIDGE_ORDER_SUBMITTED",
        )
        self._record_event(
            session,
            trace_id,
            order,
            "ORDER_ACCEPTED",
            {
                "broker_order_id": broker_order_id,
                "client_order_id": order.client_order_id,
                "mode": self.config.mode,
            },
        )
        return BrokerOrderResult(
            accepted=True,
            broker_order_id=broker_order_id,
            message=f"Order accepted by Longbridge {self.config.mode} account",
        )

    def cancel_order(
        self,
        session: Session,
        trace_id: str,
        order: Order,
    ) -> BrokerOrderResult:
        """请求撤单；最终 CANCELLED 状态等待查询或推送确认。"""

        if order.status not in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
            return BrokerOrderResult(
                accepted=False,
                broker_order_id=order.broker_order_id,
                message=f"Order status {order.status} is not cancellable",
            )
        if order.broker_order_id is None:
            return BrokerOrderResult(accepted=False, message="Missing broker order id")

        self.order_service.transition_order(
            session,
            trace_id,
            order,
            OrderStatus.CANCEL_PENDING,
            "LONGBRIDGE_CANCEL_PENDING",
        )
        try:
            self.gateway.cancel_order(order.broker_order_id)
        except (TimeoutError, ConnectionError) as exc:
            self.order_service.transition_order(
                session,
                trace_id,
                order,
                OrderStatus.RECONCILING,
                "LONGBRIDGE_CANCEL_RESULT_UNKNOWN",
            )
            return BrokerOrderResult(
                accepted=False,
                broker_order_id=order.broker_order_id,
                message=f"Longbridge cancel result is unknown: {exc}",
            )
        except Exception as exc:
            self.order_service.transition_order(
                session,
                trace_id,
                order,
                OrderStatus.RECONCILING,
                "LONGBRIDGE_CANCEL_FAILED",
            )
            return BrokerOrderResult(
                accepted=False,
                broker_order_id=order.broker_order_id,
                message=f"Longbridge cancel failed: {exc}",
            )

        self._record_event(
            session,
            trace_id,
            order,
            "CANCEL_REQUESTED",
            {"broker_order_id": order.broker_order_id},
        )
        return BrokerOrderResult(
            accepted=True,
            broker_order_id=order.broker_order_id,
            message="Longbridge cancel request accepted",
        )

    def account_balances(self) -> list[BrokerAccountSnapshot]:
        return self.gateway.account_balances()

    def positions(self) -> list[BrokerPositionSnapshot]:
        return self.gateway.positions()

    def get_order(self, broker_order_id: str) -> BrokerOrderSnapshot:
        return self.gateway.order_detail(broker_order_id)

    def list_orders(self) -> list[BrokerOrderSnapshot]:
        return self.gateway.list_orders()

    def quote(self, symbol: str) -> MarketDataSnapshot:
        return self.gateway.quote(symbol)

    def handle_order_event(
        self,
        session: Session,
        payload: dict,
        *,
        trace_id: str | None = None,
    ) -> Order | None:
        """保存长桥交易推送，并按 Broker 状态推进本地订单状态机。"""

        broker_order_id = self._payload_value(payload, "order_id", "broker_order_id")
        event_trace_id = trace_id or new_id("trace_push")
        order = session.scalar(
            select(Order).where(Order.broker_order_id == str(broker_order_id))
        )
        session.add(
            BrokerEvent(
                broker_event_id=new_id("broker_evt"),
                trace_id=event_trace_id,
                broker_name=self.broker_name,
                broker_order_id=str(broker_order_id) if broker_order_id is not None else None,
                broker_fill_id=self._optional_string(
                    self._payload_value(payload, "trade_id", "broker_fill_id")
                ),
                event_type="ORDER_CHANGED",
                raw_payload=payload,
                processed_at=datetime.now(UTC),
            )
        )
        if order is None:
            return None

        target_status = self._map_order_status(
            self._payload_value(payload, "status", "order_status")
        )
        if target_status is not None and target_status != order.status:
            self.order_service.transition_order(
                session,
                event_trace_id,
                order,
                target_status,
                "LONGBRIDGE_ORDER_PUSH",
            )
            if target_status == OrderStatus.FILLED:
                self.order_service.enqueue_event(
                    session,
                    event_trace_id,
                    "ORDER_FILLED",
                    "ORDER",
                    order.order_id,
                    {"order_id": order.order_id, "broker_order_id": order.broker_order_id},
                )
            elif target_status == OrderStatus.CANCELLED:
                self.order_service.enqueue_event(
                    session,
                    event_trace_id,
                    "ORDER_CANCELLED",
                    "ORDER",
                    order.order_id,
                    {"order_id": order.order_id, "broker_order_id": order.broker_order_id},
                )
        return order

    def subscribe_order_events(
        self,
        session_factory: Callable[[], Session],
    ) -> None:
        """把 SDK 推送转换为短事务处理，避免在回调中长期持有 Session。"""

        def handler(payload: dict) -> None:
            session = session_factory()
            try:
                self.handle_order_event(session, payload)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        self.gateway.subscribe_order_events(handler)

    def subscribe_quote_events(
        self,
        symbols: list[str],
        handler: Callable[[dict], None],
    ) -> None:
        self.gateway.subscribe_quote_events(symbols, handler)

    def _submission_safety_error(self) -> str | None:
        mode = self.config.mode.lower()
        if mode not in {"paper", "live"}:
            return f"Unsupported Longbridge mode: {self.config.mode}"
        if not self.config.enable_order_submission:
            return "External order submission is disabled"
        if mode == "live" and not self.config.enable_live_trading:
            return "Live trading is disabled"
        return None

    @staticmethod
    def _map_order_status(value: object) -> OrderStatus | None:
        if value is None:
            return None
        normalized = "".join(character for character in str(value).upper() if character.isalnum())
        mapping = {
            "NEW": OrderStatus.SUBMITTED,
            "NOTREPORTED": OrderStatus.SUBMITTING,
            "REPLACEDNOTREPORTED": OrderStatus.SUBMITTING,
            "PROTECTEDNOTREPORTED": OrderStatus.SUBMITTING,
            "VARIETIESNOTREPORTED": OrderStatus.SUBMITTING,
            "WAITTONEW": OrderStatus.SUBMITTING,
            "PENDINGNEW": OrderStatus.SUBMITTING,
            "WAITTOREPLACE": OrderStatus.SUBMITTED,
            "PENDINGREPLACE": OrderStatus.SUBMITTED,
            "REPLACED": OrderStatus.SUBMITTED,
            "PARTIALFILLED": OrderStatus.PARTIALLY_FILLED,
            "PARTIALLYFILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "WAITTOCANCEL": OrderStatus.CANCEL_PENDING,
            "PENDINGCANCEL": OrderStatus.CANCEL_PENDING,
            "CANCELED": OrderStatus.CANCELLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.FAILED,
            "EXPIRED": OrderStatus.CANCELLED,
            "PARTIALWITHDRAWAL": OrderStatus.CANCELLED,
        }
        direct_match = mapping.get(normalized)
        if direct_match is not None:
            return direct_match
        for status_name, status in mapping.items():
            if normalized.endswith(status_name):
                return status
        return None

    def _record_event(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        event_type: str,
        raw_payload: dict,
    ) -> None:
        session.add(
            BrokerEvent(
                broker_event_id=new_id("broker_evt"),
                trace_id=trace_id,
                broker_name=self.broker_name,
                broker_order_id=order.broker_order_id,
                event_type=event_type,
                raw_payload=raw_payload,
                processed_at=datetime.now(UTC),
            )
        )

    @staticmethod
    def _payload_value(payload: dict, *names: str):
        for name in names:
            if name in payload:
                return payload[name]
        return None

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return None if value is None else str(value)
