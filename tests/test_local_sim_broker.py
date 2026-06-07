from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from synapse_yield.broker.local_sim import LocalSimBroker
from synapse_yield.domain.enums import OrderSide, OrderStatus, OrderType, OutboxStatus
from synapse_yield.domain.schemas import LocalSimConfig, MarketQuote, OrderIntent
from synapse_yield.harness.order_service import OrderService
from synapse_yield.storage.base import Base
from synapse_yield.storage.models import (
    Account,
    AuditLog,
    BrokerEvent,
    CashLedger,
    Fill,
    OutboxEvent,
    Position,
)


def _session() -> Session:
    """为每个测试创建隔离的内存数据库，避免案例之间共享交易数据。"""

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def test_local_sim_buy_fill_updates_order_account_position_and_cash_ledger() -> None:
    """验证立即成交买单会同步更新订单、现金、持仓、成交和资金流水。"""

    session = _session()
    trace_id = "trace_local_sim_buy"
    account_id = "acct_1"
    symbol = "AAPL.US"
    order_service = OrderService()
    broker = LocalSimBroker(order_service=order_service)

    account = Account(
        account_id=account_id,
        base_currency="USD",
        cash_available=Decimal("100000"),
        cash_frozen=Decimal("0"),
        equity=Decimal("100000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    session.add(account)
    session.flush()

    intent = OrderIntent(
        account_id=account_id,
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("180.10"),
    )
    order = order_service.create_order_from_intent(session, trace_id, intent)
    risk_decision = order_service.run_risk_check(session, trace_id, order)

    assert risk_decision.decision.value == "APPROVED"
    assert order.status == OrderStatus.RISK_APPROVED

    result = broker.submit_order(
        session,
        trace_id,
        order,
        MarketQuote(
            symbol=symbol,
            last_price=Decimal("180.00"),
            bid_price=Decimal("179.95"),
            ask_price=Decimal("180.05"),
        ),
    )
    session.flush()

    assert result.accepted is True
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == Decimal("10")
    assert order.avg_fill_price == Decimal("180.05")
    assert account.cash_available == Decimal("98199.50")
    assert account.equity == Decimal("100000.00")

    position = session.scalar(
        select(Position).where(Position.account_id == account_id, Position.symbol == symbol)
    )
    assert position is not None
    assert position.quantity == Decimal("10")
    assert position.available_quantity == Decimal("10")
    assert position.avg_cost == Decimal("180.05")
    assert position.market_value == Decimal("1800.50")

    fill = session.scalar(select(Fill).where(Fill.order_id == order.order_id))
    assert fill is not None
    assert fill.quantity == Decimal("10")
    assert fill.price == Decimal("180.05")

    cash_ledger = session.scalar(select(CashLedger).where(CashLedger.order_id == order.order_id))
    assert cash_ledger is not None
    assert cash_ledger.fill_id == fill.fill_id
    assert cash_ledger.event_type == "BUY_FILL"
    assert cash_ledger.amount == Decimal("-1800.50")
    assert cash_ledger.balance_after == Decimal("98199.50")

    outbox_event = session.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == order.order_id))
    assert outbox_event is not None
    assert outbox_event.event_type == "ORDER_READY_TO_SUBMIT"
    assert outbox_event.status == OutboxStatus.PENDING


def test_unmatched_limit_order_reserves_cash_then_fills_on_new_quote() -> None:
    """验证未成交限价买单先冻结现金，并可被后续行情撮合。"""

    session = _session()
    account = Account(
        account_id="acct_pending",
        base_currency="USD",
        cash_available=Decimal("10000"),
        cash_frozen=Decimal("0"),
        equity=Decimal("10000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    session.add(account)
    order_service = OrderService()
    broker = LocalSimBroker(order_service)
    order = order_service.create_order_from_intent(
        session,
        "trace_pending",
        OrderIntent(
            account_id=account.account_id,
            symbol="AAPL.US",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("10"),
            limit_price=Decimal("90"),
        ),
    )
    order_service.run_risk_check(session, "trace_pending", order)

    broker.submit_order(
        session,
        "trace_pending",
        order,
        MarketQuote(
            symbol="AAPL.US",
            last_price=Decimal("100"),
            bid_price=Decimal("99"),
            ask_price=Decimal("100"),
        ),
    )
    assert order.status == OrderStatus.SUBMITTED
    assert account.cash_available == Decimal("9100")
    assert account.cash_frozen == Decimal("900")

    filled_ids = broker.process_quote(
        session,
        "trace_pending",
        MarketQuote(
            symbol="AAPL.US",
            last_price=Decimal("89"),
            bid_price=Decimal("88.90"),
            ask_price=Decimal("89"),
        ),
    )
    session.flush()

    assert filled_ids == [order.order_id]
    assert order.status == OrderStatus.FILLED
    assert account.cash_frozen == Decimal("0")
    assert account.cash_available == Decimal("9110")
    assert session.scalar(select(Fill).where(Fill.order_id == order.order_id)) is not None


def test_cancel_unmatched_buy_order_releases_frozen_cash() -> None:
    """验证撤销未成交买单后，冻结现金会完整恢复。"""

    session = _session()
    account = Account(
        account_id="acct_cancel",
        base_currency="USD",
        cash_available=Decimal("10000"),
        cash_frozen=Decimal("0"),
        equity=Decimal("10000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    session.add(account)
    order_service = OrderService()
    broker = LocalSimBroker(order_service)
    order = order_service.create_order_from_intent(
        session,
        "trace_cancel",
        OrderIntent(
            account_id=account.account_id,
            symbol="AAPL.US",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("10"),
            limit_price=Decimal("90"),
        ),
    )
    order_service.run_risk_check(session, "trace_cancel", order)
    broker.submit_order(
        session,
        "trace_cancel",
        order,
        MarketQuote(symbol="AAPL.US", last_price=Decimal("100"), ask_price=Decimal("100")),
    )

    result = broker.cancel_order(session, "trace_cancel", order)
    session.flush()

    assert result.accepted is True
    assert order.status == OrderStatus.CANCELLED
    assert account.cash_available == Decimal("10000")
    assert account.cash_frozen == Decimal("0")
    cancelled_event = session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == order.order_id,
            OutboxEvent.event_type == "ORDER_CANCELLED",
        )
    )
    assert cancelled_event is not None


def test_sell_fill_updates_realized_pnl_and_available_position() -> None:
    """验证卖出成交会减少持仓并确认已实现盈亏。"""

    session = _session()
    account = Account(
        account_id="acct_sell",
        base_currency="USD",
        cash_available=Decimal("1000"),
        cash_frozen=Decimal("0"),
        equity=Decimal("2000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("200"),
    )
    position = Position(
        account_id=account.account_id,
        symbol="AAPL.US",
        quantity=Decimal("10"),
        available_quantity=Decimal("10"),
        avg_cost=Decimal("80"),
        market_price=Decimal("100"),
        market_value=Decimal("1000"),
        unrealized_pnl=Decimal("200"),
    )
    session.add_all([account, position])
    order_service = OrderService()
    broker = LocalSimBroker(order_service)
    order = order_service.create_order_from_intent(
        session,
        "trace_sell",
        OrderIntent(
            account_id=account.account_id,
            symbol="AAPL.US",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=Decimal("5"),
            limit_price=Decimal("110"),
        ),
    )
    order_service.run_risk_check(session, "trace_sell", order)
    broker.submit_order(
        session,
        "trace_sell",
        order,
        MarketQuote(
            symbol="AAPL.US",
            last_price=Decimal("110"),
            bid_price=Decimal("110"),
            ask_price=Decimal("110.10"),
        ),
    )
    session.flush()

    assert order.status == OrderStatus.FILLED
    assert position.quantity == Decimal("5")
    assert position.available_quantity == Decimal("5")
    assert position.unrealized_pnl == Decimal("150")
    assert account.cash_available == Decimal("1550")
    assert account.realized_pnl == Decimal("150")
    assert account.equity == Decimal("2100")


def test_commission_is_applied_to_cash_cost_and_position_cost_basis() -> None:
    """验证佣金同时影响现金支出、账户权益和持仓成本。"""

    session = _session()
    account = Account(
        account_id="acct_fee",
        base_currency="USD",
        cash_available=Decimal("10000"),
        cash_frozen=Decimal("0"),
        equity=Decimal("10000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    session.add(account)
    order_service = OrderService()
    broker = LocalSimBroker(
        order_service,
        LocalSimConfig(commission_rate=Decimal("0.001"), minimum_commission=Decimal("1")),
    )
    order = order_service.create_order_from_intent(
        session,
        "trace_fee",
        OrderIntent(
            account_id=account.account_id,
            symbol="AAPL.US",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("10"),
            limit_price=Decimal("100"),
        ),
    )
    order_service.run_risk_check(session, "trace_fee", order)
    broker.submit_order(
        session,
        "trace_fee",
        order,
        MarketQuote(symbol="AAPL.US", last_price=Decimal("100"), ask_price=Decimal("100")),
    )
    session.flush()

    fill = session.scalar(select(Fill).where(Fill.order_id == order.order_id))
    position = session.scalar(
        select(Position).where(
            Position.account_id == account.account_id,
            Position.symbol == "AAPL.US",
        )
    )
    assert fill is not None
    assert position is not None
    assert fill.commission == Decimal("1")
    assert account.cash_available == Decimal("8999")
    assert account.equity == Decimal("9999")
    assert position.avg_cost == Decimal("100.1")
    assert position.unrealized_pnl == Decimal("-1")


def test_local_sim_persists_broker_events_and_fill_outbox_events() -> None:
    """验证接单和成交会留下 Broker、Outbox 与审计三类事件。"""

    session = _session()
    account = Account(
        account_id="acct_events",
        base_currency="USD",
        cash_available=Decimal("10000"),
        cash_frozen=Decimal("0"),
        equity=Decimal("10000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    session.add(account)
    order_service = OrderService()
    broker = LocalSimBroker(order_service)
    order = order_service.create_order_from_intent(
        session,
        "trace_events",
        OrderIntent(
            account_id=account.account_id,
            symbol="AAPL.US",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("2"),
        ),
    )
    quote = MarketQuote(symbol="AAPL.US", last_price=Decimal("100"), ask_price=Decimal("101"))
    order_service.run_risk_check(session, "trace_events", order, quote=quote)
    broker.submit_order(session, "trace_events", order, quote)
    session.flush()

    broker_events = session.scalars(
        select(BrokerEvent).where(BrokerEvent.broker_order_id == order.broker_order_id)
    ).all()
    outbox_types = set(
        session.scalars(
            select(OutboxEvent.event_type).where(OutboxEvent.trace_id == "trace_events")
        ).all()
    )
    audit_types = set(
        session.scalars(
            select(AuditLog.event_type).where(AuditLog.trace_id == "trace_events")
        ).all()
    )
    assert {event.event_type for event in broker_events} == {"ORDER_ACCEPTED", "FILL"}
    assert {"ORDER_READY_TO_SUBMIT", "ORDER_FILLED", "POSITION_UPDATED"} <= outbox_types
    assert {
        "ORDER_CREATED",
        "RISK_CHECK_COMPLETED",
        "LOCAL_SIM_RESOURCES_RESERVED",
        "LOCAL_SIM_ORDER_SUBMITTED",
        "LOCAL_SIM_ORDER_FILLED",
        "LOCAL_SIM_LEDGER_UPDATED",
    } <= audit_types
