from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from synapse_yield.agents.graph import AgentGraphConfig, TradingAgentGraph
from synapse_yield.broker.local_sim import LocalSimBroker
from synapse_yield.domain.enums import OrderSide, OrderStatus, OrderType
from synapse_yield.domain.schemas import MarketDataSnapshot, StrategyOutput
from synapse_yield.harness.order_service import OrderService
from synapse_yield.storage.base import Base
from synapse_yield.storage.models import Account, Fill, Order, RiskDecision
from synapse_yield.strategy.adapters import CallableStrategyAdapter
from synapse_yield.strategy.runner import StrategyRunner


def _session() -> Session:
    """为每个 Agent 图测试创建隔离数据库。"""

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def _account(session: Session, cash: str = "10000") -> Account:
    """创建四 Agent 共用的测试账户。"""

    account = Account(
        account_id="acct_agents",
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


def _snapshot() -> MarketDataSnapshot:
    """构造标准行情输入。"""

    return MarketDataSnapshot(
        symbol="AAPL.US",
        timestamp=datetime(2026, 6, 8, 9, 30, tzinfo=UTC),
        last_price=Decimal("100"),
        bid_price=Decimal("99.99"),
        ask_price=Decimal("100"),
        source="test",
    )


def _adapter():
    """构造确定性市价买入策略。"""

    return CallableStrategyAdapter(
        "agent_buy",
        "v1",
        lambda snapshot, context: StrategyOutput(
            side=OrderSide.BUY,
            confidence=Decimal("0.9"),
            target_quantity=Decimal("2"),
            order_type=OrderType.MARKET,
            reason="agent_graph_test",
        ),
    )


def _graph() -> TradingAgentGraph:
    """显式使用本地 Broker，避免开发者 .env 改变单元测试语义。"""

    order_service = OrderService()
    return TradingAgentGraph(
        AgentGraphConfig(
            strategy_runner=StrategyRunner(),
            order_service=order_service,
            broker=LocalSimBroker(order_service=order_service),
        )
    )


@pytest.fixture(autouse=True)
def disable_langsmith_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试只验证本地图，不向 LangSmith 发送追踪数据。"""

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


def test_four_agents_execute_in_order_and_fill_order() -> None:
    """验证四个 Agent 按 Market、Strategy、Risk、Execution 顺序执行。"""

    session = _session()
    account = _account(session)
    result = _graph().run_market_snapshot(
        session,
        _adapter(),
        _snapshot(),
        account_id=account.account_id,
        trace_id="trace_agent_graph",
        market_is_open=True,
    )
    session.flush()

    assert result.agent_path == ("market", "strategy", "risk", "execution")
    assert result.order_status == OrderStatus.FILLED
    assert session.scalar(select(Order)) is not None
    assert session.scalar(select(RiskDecision)) is not None
    assert session.scalar(select(Fill)) is not None


def test_dry_run_stops_after_strategy_agent() -> None:
    """dry run 在 Strategy Agent 后结束，不进入风控和执行节点。"""

    session = _session()
    account = _account(session)
    result = _graph().run_market_snapshot(
        session,
        _adapter(),
        _snapshot(),
        account_id=account.account_id,
        trace_id="trace_agent_dry_run",
        dry_run=True,
    )

    assert result.agent_path == ("market", "strategy")
    assert result.dry_run is True
    assert session.scalar(select(Order)) is None


def test_risk_rejection_stops_before_execution_agent() -> None:
    """资金不足时 Risk Agent 拒绝订单，Execution Agent 不得运行。"""

    session = _session()
    account = _account(session, cash="50")
    result = _graph().run_market_snapshot(
        session,
        _adapter(),
        _snapshot(),
        account_id=account.account_id,
        trace_id="trace_agent_rejected",
        market_is_open=True,
    )

    assert result.agent_path == ("market", "strategy", "risk")
    assert result.order_status == OrderStatus.RISK_REJECTED
    assert session.scalar(select(Fill)) is None


def test_langgraph_entrypoint_returns_filled_result() -> None:
    """生产入口应通过编译后的 StateGraph 返回最终结果。"""

    session = _session()
    account = _account(session)

    result = _graph().run_market_snapshot(
        session,
        _adapter(),
        _snapshot(),
        account_id=account.account_id,
        trace_id="trace_real_langgraph",
        market_is_open=True,
    )

    assert result.order_status == OrderStatus.FILLED
