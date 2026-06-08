from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from synapse_yield.domain.enums import OrderSide, OrderStatus, OrderType
from synapse_yield.domain.schemas import MarketDataSnapshot, StrategyOutput
from synapse_yield.harness.trading import TradingHarness
from synapse_yield.storage.base import Base
from synapse_yield.storage.models import (
    Account,
    AuditLog,
    Fill,
    Order,
    OutboxEvent,
    RiskDecision,
    StrategySignal,
)
from synapse_yield.strategy.adapters import CallableStrategyAdapter


def _session() -> Session:
    """为每个 Harness 编排测试创建隔离数据库。"""

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def _account(session: Session, cash: str = "10000") -> Account:
    """创建用于端到端交易编排的测试账户。"""

    account = Account(
        account_id="acct_harness",
        base_currency="USD",
        cash_available=Decimal(cash),
        cash_frozen=Decimal("0"),
        equity=Decimal(cash),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    session.add(account)
    session.flush()
    return account


def _snapshot(price: str = "100") -> MarketDataSnapshot:
    """构造同时可供策略、风控和 Broker 使用的标准行情。"""

    return MarketDataSnapshot(
        symbol="AAPL.US",
        timestamp=datetime(2026, 6, 8, 9, 30, tzinfo=UTC),
        last_price=Decimal(price),
        bid_price=Decimal(price) - Decimal("0.01"),
        ask_price=Decimal(price),
        source="test",
    )


def _buy_adapter(quantity: str = "2") -> CallableStrategyAdapter:
    """构造一个始终输出市价买入信号的测试策略。"""

    return CallableStrategyAdapter(
        "always_buy",
        "v1",
        lambda snapshot, context: StrategyOutput(
            side=OrderSide.BUY,
            confidence=Decimal("0.9"),
            target_quantity=Decimal(quantity),
            order_type=OrderType.MARKET,
            reason="harness_test_buy",
        ),
    )


def test_harness_runs_strategy_risk_and_local_broker_to_fill_order() -> None:
    """验证第四阶段完整链路：行情、策略、风控、订单和执行。"""

    session = _session()
    account = _account(session)
    harness = TradingHarness()

    result = harness.run_market_snapshot(
        session,
        _buy_adapter(),
        _snapshot("100"),
        account_id=account.account_id,
        trace_id="trace_harness_fill",
        market_is_open=True,
    )
    session.flush()

    order = session.get(Order, result.order_id)
    assert result.order_status == OrderStatus.FILLED
    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.source_signal_id == result.signal_id
    assert session.scalar(select(StrategySignal).where(StrategySignal.signal_id == result.signal_id))
    assert session.scalar(select(RiskDecision).where(RiskDecision.risk_decision_id == result.risk_decision_id))
    assert session.scalar(select(Fill).where(Fill.order_id == order.order_id))
    assert account.cash_available == Decimal("9800")


def test_harness_dry_run_stops_after_strategy_signal() -> None:
    """dry run 只保存策略信号，不创建订单、不触发风控或 Broker。"""

    session = _session()
    account = _account(session)

    result = TradingHarness().run_market_snapshot(
        session,
        _buy_adapter(),
        _snapshot("100"),
        account_id=account.account_id,
        trace_id="trace_harness_dry_run",
        dry_run=True,
    )
    session.flush()

    assert result.dry_run is True
    assert result.signal_id is not None
    assert result.order_id is None
    assert session.scalar(select(Order)) is None
    assert session.scalar(select(RiskDecision)) is None


def test_harness_returns_risk_rejected_order_without_broker_submit() -> None:
    """风控拒绝时订单停在 RISK_REJECTED，不会提交给 Broker。"""

    session = _session()
    account = _account(session, cash="50")

    result = TradingHarness().run_market_snapshot(
        session,
        _buy_adapter(),
        _snapshot("100"),
        account_id=account.account_id,
        trace_id="trace_harness_rejected",
        market_is_open=True,
    )
    session.flush()

    order = session.get(Order, result.order_id)
    assert order is not None
    assert result.order_status == OrderStatus.RISK_REJECTED
    assert order.status == OrderStatus.RISK_REJECTED
    assert order.broker_order_id is None
    assert session.scalar(select(Fill)) is None


def test_harness_reuses_existing_order_on_idempotency_hit() -> None:
    """相同 trace、策略和信号再次进入时，不会创建或提交第二张订单。"""

    session = _session()
    account = _account(session)
    harness = TradingHarness()
    snapshot = _snapshot("100")

    first = harness.run_market_snapshot(
        session,
        _buy_adapter(),
        snapshot,
        account_id=account.account_id,
        trace_id="trace_harness_idempotent",
        market_is_open=True,
    )
    second = harness.run_market_snapshot(
        session,
        _buy_adapter(),
        snapshot,
        account_id=account.account_id,
        trace_id="trace_harness_idempotent",
        market_is_open=True,
    )
    session.flush()

    orders = session.scalars(select(Order)).all()
    fills = session.scalars(select(Fill)).all()
    audit_types = session.scalars(select(AuditLog.event_type)).all()
    assert first.order_id == second.order_id
    assert len(orders) == 1
    assert len(fills) == 1
    assert "ORDER_IDEMPOTENCY_HIT" in audit_types


def test_harness_emits_trace_linked_outbox_events() -> None:
    """编排链路产生的 Outbox 事件应全部带有同一个 trace id。"""

    session = _session()
    account = _account(session)

    result = TradingHarness().run_market_snapshot(
        session,
        _buy_adapter(),
        _snapshot("100"),
        account_id=account.account_id,
        trace_id="trace_harness_outbox",
        market_is_open=True,
    )
    session.flush()

    events = session.scalars(
        select(OutboxEvent).where(OutboxEvent.trace_id == result.trace_id)
    ).all()
    assert {event.event_type for event in events} >= {
        "ORDER_READY_TO_SUBMIT",
        "ORDER_FILLED",
        "POSITION_UPDATED",
    }
