from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from synapse_yield.domain.enums import OrderSide, OrderType
from synapse_yield.domain.schemas import (
    MarketDataSnapshot,
    StrategyContext,
    StrategyOutput,
)
from synapse_yield.storage.base import Base
from synapse_yield.storage.models import MarketSnapshot, Order, StrategySignal
from synapse_yield.strategy.adapters import CallableStrategyAdapter
from synapse_yield.strategy.examples import PriceMoveStrategy
from synapse_yield.strategy.runner import StrategyRunner


def _session() -> Session:
    """为每个策略测试创建独立的内存数据库。"""

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def _snapshot(price: str, offset_minutes: int = 0) -> MarketDataSnapshot:
    """构造带固定时间顺序的标准行情快照。"""

    return MarketDataSnapshot(
        symbol="AAPL.US",
        timestamp=datetime(2026, 6, 7, 9, 30, tzinfo=UTC)
        + timedelta(minutes=offset_minutes),
        last_price=Decimal(price),
        bid_price=Decimal(price) - Decimal("0.01"),
        ask_price=Decimal(price) + Decimal("0.01"),
        source="test",
    )


def test_callable_adapter_standardizes_dict_output() -> None:
    """旧策略返回字典时，Adapter 应转换成标准 StrategyOutput。"""

    def legacy_strategy(snapshot: MarketDataSnapshot, context: StrategyContext) -> dict:
        return {
            "side": "BUY",
            "confidence": "0.8",
            "target_quantity": "2",
            "order_type": "LIMIT",
            "limit_price": str(snapshot.last_price),
            "reason": "legacy_strategy_signal",
        }

    adapter = CallableStrategyAdapter("legacy", "v1", legacy_strategy)
    output = adapter.generate_signal(
        _snapshot("100"),
        StrategyContext(account_id="acct_strategy"),
    )

    assert isinstance(output, StrategyOutput)
    assert output.side == OrderSide.BUY
    assert output.limit_price == Decimal("100")


def test_dry_run_persists_snapshot_and_signal_without_order_intent() -> None:
    """dry run 应记录策略结果，但不能生成可交给 Harness 的订单意图。"""

    session = _session()
    runner = StrategyRunner()
    adapter = CallableStrategyAdapter(
        "always_buy",
        "v1",
        lambda snapshot, context: StrategyOutput(
            side=OrderSide.BUY,
            confidence=Decimal("0.9"),
            target_quantity=Decimal("3"),
            order_type=OrderType.MARKET,
            reason="test_signal",
        ),
    )

    result = runner.run(
        session,
        "trace_dry_run",
        adapter,
        _snapshot("100"),
        StrategyContext(account_id="acct_strategy"),
        dry_run=True,
    )
    session.flush()

    assert result.signal_id is not None
    assert result.order_intent is None
    assert session.get(MarketSnapshot, result.snapshot_id) is not None
    signal = session.get(StrategySignal, result.signal_id)
    assert signal is not None
    assert signal.raw_payload["dry_run"] is True
    assert session.scalar(select(Order)) is None


def test_non_dry_run_returns_standard_order_intent_without_creating_order() -> None:
    """非 dry run 只生成订单意图，订单创建仍由第四阶段 Harness 负责。"""

    session = _session()
    runner = StrategyRunner()
    adapter = CallableStrategyAdapter(
        "limit_buy",
        "v2",
        lambda snapshot, context: {
            "side": "BUY",
            "confidence": "0.75",
            "target_quantity": "5",
            "order_type": "LIMIT",
            "limit_price": "99.5",
            "reason": "price_entry",
        },
    )

    result = runner.run(
        session,
        "trace_live_intent",
        adapter,
        _snapshot("100"),
        StrategyContext(account_id="acct_strategy"),
        dry_run=False,
    )
    session.flush()

    assert result.order_intent is not None
    assert result.order_intent.account_id == "acct_strategy"
    assert result.order_intent.source_signal_id == result.signal_id
    assert result.order_intent.strategy_name == "limit_buy"
    assert session.scalar(select(Order)) is None


def test_price_move_strategy_needs_history_and_respects_threshold() -> None:
    """示例策略应在价格变化达到阈值后才产生方向信号。"""

    adapter = PriceMoveStrategy()
    first = _snapshot("100")
    second = _snapshot("102", offset_minutes=1)

    assert adapter.generate_signal(
        first,
        StrategyContext(account_id="acct_strategy", parameters={"threshold": "0.01"}),
    ) is None

    output = adapter.generate_signal(
        second,
        StrategyContext(
            account_id="acct_strategy",
            previous_snapshots=(first,),
            parameters={"threshold": "0.01", "quantity": "4"},
        ),
    )
    assert output is not None
    assert output.side == OrderSide.BUY
    assert output.target_quantity == Decimal("4")


def test_replay_orders_snapshots_by_time_and_persists_generated_signals() -> None:
    """回放应按时间排序，并把此前行情传给后续策略调用。"""

    session = _session()
    runner = StrategyRunner()
    snapshots = [
        _snapshot("103", offset_minutes=2),
        _snapshot("100", offset_minutes=0),
        _snapshot("102", offset_minutes=1),
    ]

    result = runner.replay(
        session,
        "trace_replay",
        PriceMoveStrategy(),
        snapshots,
        account_id="acct_strategy",
        parameters={"threshold": "0.01", "quantity": "2"},
        dry_run=True,
    )
    session.flush()

    persisted_snapshots = session.scalars(
        select(MarketSnapshot).order_by(MarketSnapshot.timestamp)
    ).all()
    persisted_signals = session.scalars(select(StrategySignal)).all()
    assert result.processed_snapshots == 3
    assert result.generated_signals == 1
    assert [snapshot.last_price for snapshot in persisted_snapshots] == [
        Decimal("100"),
        Decimal("102"),
        Decimal("103"),
    ]
    assert len(persisted_signals) == 1
