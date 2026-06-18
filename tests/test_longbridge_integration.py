from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from synapse_yield.broker.factory import build_broker
from synapse_yield.broker.longbridge.adapter import LongbridgeBroker
from synapse_yield.config import get_settings
from synapse_yield.domain.enums import OrderSide, OrderStatus, OrderType
from synapse_yield.domain.schemas import StrategyOutput
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


