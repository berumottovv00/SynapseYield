from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from synapse_yield.broker.factory import build_broker
from synapse_yield.broker.longbridge.adapter import LongbridgeBroker
from synapse_yield.config import get_settings
from synapse_yield.domain.enums import OrderSide, OrderStatus, OrderType
from synapse_yield.domain.schemas import StrategyOutput
from synapse_yield.harness.trading import TradingHarness
from synapse_yield.storage.base import Base
from synapse_yield.storage.models import Account, Order
from synapse_yield.strategy.adapters import CallableStrategyAdapter


def _integration_broker() -> LongbridgeBroker:
    """从项目 .env 创建长桥 Broker，并确认没有退回本地模拟盘。"""

    get_settings.cache_clear()
    broker = build_broker()
    if not isinstance(broker, LongbridgeBroker):
        pytest.fail("Set BROKER_TYPE=longbridge before running Longbridge integration tests")
    return broker


def _session() -> Session:
    """联调订单使用独立内存库，不污染项目 MySQL 数据。"""

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


@pytest.mark.longbridge_integration
@pytest.mark.skipif(
    not get_settings().run_longbridge_integration,
    reason="Set RUN_LONGBRIDGE_INTEGRATION=true to call Longbridge read-only APIs",
)
def test_longbridge_read_only_connection() -> None:
    """验证认证、资金、持仓、订单和行情查询均可调用。"""

    broker = _integration_broker()

    balances = broker.account_balances()
    positions = broker.positions()
    orders = broker.list_orders()
    quote = broker.quote(get_settings().longbridge_test_symbol)

    assert balances, "Longbridge returned no account balance"
    assert quote.last_price > 0
    assert isinstance(positions, list)
    assert isinstance(orders, list)

    print("balances:", balances)
    print("positions:", positions)
    print("orders:", orders)
    print("quote:", quote)


@pytest.mark.longbridge_integration
@pytest.mark.longbridge_paper_order
@pytest.mark.skipif(
    not get_settings().run_longbridge_paper_order,
    reason="Set RUN_LONGBRIDGE_PAPER_ORDER=true to submit one paper order",
)
def test_longbridge_paper_limit_order_and_cancel() -> None:
    """向长桥模拟账户提交 1 股低价限价单，并在接收后尝试撤单。"""

    settings = get_settings()
    assert settings.longbridge_mode.lower() == "paper"
    assert settings.enable_external_order_submission is True
    assert settings.enable_live_trading is False

    broker = _integration_broker()
    symbol = settings.longbridge_test_symbol
    snapshot = broker.quote(symbol)
    # 使用低于最新价 2% 的限价，既满足当前 3% 风控偏离限制，也降低立即成交概率。
    limit_price = (snapshot.last_price * Decimal("0.98")).quantize(Decimal("0.01"))
    strategy = CallableStrategyAdapter(
        "longbridge_paper_integration",
        "v1",
        lambda market_snapshot, context: StrategyOutput(
            side=OrderSide.BUY,
            confidence=Decimal("1"),
            target_quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            reason="paper_account_integration_test",
        ),
    )

    session = _session()
    remote_balance = broker.account_balances()[0]
    account = Account(
        account_id="longbridge_paper_integration",
        base_currency=remote_balance.currency or "USD",
        cash_available=remote_balance.available_cash,
        cash_frozen=Decimal("0"),
        equity=remote_balance.net_assets,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    session.add(account)
    session.flush()

    result = TradingHarness(broker=broker).run_market_snapshot(
        session,
        strategy,
        snapshot,
        account_id=account.account_id,
        trace_id="trace_longbridge_paper_integration",
        market_is_open=True,
    )
    session.flush()

    assert result.broker_order_id is not None
    assert result.order_status == OrderStatus.SUBMITTED
    order = session.get(Order, result.order_id)
    assert order is not None

    broker_snapshot = broker.get_order(result.broker_order_id)
    print("submitted order:", broker_snapshot)

    # 清理测试委托。若订单已被市场成交，长桥可能拒绝撤单，此时打印结果供人工确认。
    cancel_result = broker.cancel_order(session, result.trace_id, order)
    print("cancel result:", cancel_result)
