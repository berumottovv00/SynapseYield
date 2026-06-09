from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from synapse_yield.broker.factory import build_broker
from synapse_yield.broker.local_sim import LocalSimBroker
from synapse_yield.broker.longbridge.adapter import (
    LongbridgeBroker,
    LongbridgeBrokerConfig,
)
from synapse_yield.broker.longbridge.types import (
    BrokerAccountSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    LongbridgeSubmitRequest,
)
from synapse_yield.config import Settings
from synapse_yield.domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from synapse_yield.domain.schemas import MarketDataSnapshot, MarketQuote
from synapse_yield.storage.base import Base
from synapse_yield.storage.models import Account, BrokerEvent, Order, OutboxEvent


class FakeLongbridgeGateway:
    """不访问网络的长桥 Gateway，用于验证 Adapter 行为。"""

    def __init__(self):
        self.submitted_requests: list[LongbridgeSubmitRequest] = []
        self.cancelled_order_ids: list[str] = []
        self.submit_error: Exception | None = None
        self.order_handler = None
        self.quote_handler = None

    def submit_order(self, request: LongbridgeSubmitRequest) -> str:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted_requests.append(request)
        return "lb_order_1"

    def cancel_order(self, broker_order_id: str) -> None:
        self.cancelled_order_ids.append(broker_order_id)

    def account_balances(self) -> list[BrokerAccountSnapshot]:
        return [
            BrokerAccountSnapshot(
                currency="USD",
                total_cash=Decimal("10000"),
                available_cash=Decimal("9000"),
                net_assets=Decimal("12000"),
            )
        ]

    def positions(self) -> list[BrokerPositionSnapshot]:
        return [
            BrokerPositionSnapshot(
                symbol="AAPL.US",
                quantity=Decimal("10"),
                available_quantity=Decimal("8"),
            )
        ]

    def order_detail(self, broker_order_id: str) -> BrokerOrderSnapshot:
        return BrokerOrderSnapshot(
            broker_order_id=broker_order_id,
            symbol="AAPL.US",
            status="Filled",
        )

    def list_orders(self) -> list[BrokerOrderSnapshot]:
        return [self.order_detail("lb_order_1")]

    def quote(self, symbol: str) -> MarketDataSnapshot:
        return MarketDataSnapshot(
            symbol=symbol,
            timestamp=datetime(2026, 6, 9, 9, 30, tzinfo=UTC),
            last_price=Decimal("100"),
            source="longbridge",
        )

    def subscribe_order_events(self, handler) -> None:
        self.order_handler = handler

    def subscribe_quote_events(self, symbols: list[str], handler) -> None:
        self.quote_handler = handler


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def _approved_order(session: Session) -> Order:
    account = Account(
        account_id="acct_longbridge",
        base_currency="USD",
        cash_available=Decimal("10000"),
        cash_frozen=Decimal("0"),
        equity=Decimal("10000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    order = Order(
        order_id="ord_longbridge",
        client_order_id="client_longbridge",
        account_id=account.account_id,
        symbol="AAPL.US",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.RISK_APPROVED,
        quantity=Decimal("2"),
        filled_quantity=Decimal("0"),
        limit_price=Decimal("100"),
        time_in_force=TimeInForce.DAY,
        idempotency_key="idem_longbridge",
        raw_request={},
    )
    session.add_all([account, order])
    session.flush()
    return order


def _quote() -> MarketQuote:
    return MarketQuote(symbol="AAPL.US", last_price=Decimal("100"))


def test_external_submission_is_disabled_by_default() -> None:
    session = _session()
    order = _approved_order(session)
    gateway = FakeLongbridgeGateway()

    result = LongbridgeBroker(gateway).submit_order(
        session, "trace_lb_blocked", order, _quote()
    )

    assert result.accepted is False
    assert order.status == OrderStatus.RISK_APPROVED
    assert gateway.submitted_requests == []


def test_paper_order_submission_maps_order_and_updates_state() -> None:
    session = _session()
    order = _approved_order(session)
    gateway = FakeLongbridgeGateway()
    broker = LongbridgeBroker(
        gateway,
        config=LongbridgeBrokerConfig(mode="paper", enable_order_submission=True),
    )

    result = broker.submit_order(session, "trace_lb_submit", order, _quote())
    session.flush()

    request = gateway.submitted_requests[0]
    assert result.accepted is True
    assert order.status == OrderStatus.SUBMITTED
    assert order.broker_order_id == "lb_order_1"
    assert request.client_order_id == order.client_order_id
    assert request.limit_price == Decimal("100")
    assert session.scalar(
        select(BrokerEvent).where(BrokerEvent.broker_order_id == "lb_order_1")
    )


def test_live_submission_requires_second_safety_switch() -> None:
    session = _session()
    order = _approved_order(session)
    gateway = FakeLongbridgeGateway()
    broker = LongbridgeBroker(
        gateway,
        config=LongbridgeBrokerConfig(
            mode="live",
            enable_order_submission=True,
            enable_live_trading=False,
        ),
    )

    result = broker.submit_order(session, "trace_lb_live_blocked", order, _quote())

    assert result.accepted is False
    assert "Live trading is disabled" in result.message
    assert gateway.submitted_requests == []


def test_submit_timeout_moves_order_to_reconciling() -> None:
    session = _session()
    order = _approved_order(session)
    gateway = FakeLongbridgeGateway()
    gateway.submit_error = TimeoutError("network timeout")
    broker = LongbridgeBroker(
        gateway,
        config=LongbridgeBrokerConfig(mode="paper", enable_order_submission=True),
    )

    result = broker.submit_order(session, "trace_lb_timeout", order, _quote())

    assert result.accepted is False
    assert order.status == OrderStatus.RECONCILING


def test_cancel_waits_for_broker_confirmation() -> None:
    session = _session()
    order = _approved_order(session)
    order.status = OrderStatus.SUBMITTED
    order.broker_order_id = "lb_order_1"
    gateway = FakeLongbridgeGateway()
    broker = LongbridgeBroker(gateway)

    result = broker.cancel_order(session, "trace_lb_cancel", order)

    assert result.accepted is True
    assert order.status == OrderStatus.CANCEL_PENDING
    assert gateway.cancelled_order_ids == ["lb_order_1"]


def test_order_push_updates_state_and_emits_outbox_event() -> None:
    session = _session()
    order = _approved_order(session)
    order.status = OrderStatus.SUBMITTED
    order.broker_order_id = "lb_order_1"
    broker = LongbridgeBroker(FakeLongbridgeGateway())

    broker.handle_order_event(
        session,
        {"order_id": "lb_order_1", "status": "Filled", "trade_id": "trade_1"},
        trace_id="trace_lb_push",
    )
    session.flush()

    assert order.status == OrderStatus.FILLED
    assert session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == order.order_id,
            OutboxEvent.event_type == "ORDER_FILLED",
        )
    )


def test_queries_are_normalized_by_gateway_boundary() -> None:
    broker = LongbridgeBroker(FakeLongbridgeGateway())

    assert broker.account_balances()[0].available_cash == Decimal("9000")
    assert broker.positions()[0].symbol == "AAPL.US"
    assert broker.get_order("lb_order_1").status == "Filled"
    assert broker.list_orders()[0].broker_order_id == "lb_order_1"
    assert broker.quote("AAPL.US").source == "longbridge"


def test_broker_factory_defaults_to_local_sim_and_supports_longbridge() -> None:
    gateway = FakeLongbridgeGateway()
    local_settings = Settings(BROKER_TYPE="local_sim")
    longbridge_settings = Settings(
        BROKER_TYPE="longbridge",
        LONGBRIDGE_MODE="paper",
        ENABLE_EXTERNAL_ORDER_SUBMISSION=True,
    )

    assert isinstance(build_broker(settings=local_settings), LocalSimBroker)
    assert isinstance(
        build_broker(settings=longbridge_settings, longbridge_gateway=gateway),
        LongbridgeBroker,
    )


def test_broker_factory_reports_missing_longbridge_environment_variables() -> None:
    settings = Settings(
        BROKER_TYPE="longbridge",
        LONGBRIDGE_APP_KEY="",
        LONGBRIDGE_APP_SECRET="",
        LONGBRIDGE_ACCESS_TOKEN="",
    )

    with pytest.raises(ValueError, match="LONGBRIDGE_APP_KEY"):
        build_broker(settings=settings)
