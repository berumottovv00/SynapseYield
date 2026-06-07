from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from synapse_yield.domain.enums import OrderSide, OrderType
from synapse_yield.domain.schemas import MarketQuote, OrderIntent, RiskConfig
from synapse_yield.risk.engine import RiskEngine
from synapse_yield.storage.base import Base
from synapse_yield.storage.models import Account, Position


def _session() -> Session:
    """为每条风控规则测试创建独立的内存数据库。"""

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def _account(session: Session, *, equity: str = "10000", realized_pnl: str = "0") -> Account:
    """创建具有可控权益和盈亏的基础测试账户。"""

    account = Account(
        account_id="acct_risk",
        base_currency="USD",
        cash_available=Decimal("10000"),
        cash_frozen=Decimal("0"),
        equity=Decimal(equity),
        realized_pnl=Decimal(realized_pnl),
        unrealized_pnl=Decimal("0"),
    )
    session.add(account)
    session.flush()
    return account


def _buy_intent(price: str = "100", quantity: str = "10") -> OrderIntent:
    """构造可复用的 AAPL 限价买入意图。"""

    return OrderIntent(
        account_id="acct_risk",
        symbol="AAPL.US",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
    )


def test_rejects_order_when_market_is_explicitly_closed() -> None:
    """交易日历明确返回休市时，风控应拒绝订单。"""

    session = _session()
    _account(session)

    result = RiskEngine().check(session, _buy_intent(), market_is_open=False)

    assert result.approved is False
    assert result.reason_code == "MARKET_CLOSED"


def test_rejects_limit_price_with_excessive_market_deviation() -> None:
    """限价偏离最新价超过阈值时，应拦截疑似异常价格。"""

    session = _session()
    _account(session)

    result = RiskEngine().check(
        session,
        _buy_intent(price="110"),
        quote=MarketQuote(symbol="AAPL.US", last_price=Decimal("100")),
    )

    assert result.approved is False
    assert result.reason_code == "LIMIT_PRICE_DEVIATION_EXCEEDED"


def test_rejects_projected_symbol_position_ratio() -> None:
    """新增订单导致单票预计仓位超限时，应拒绝买入。"""

    session = _session()
    account = _account(session)
    session.add(
        Position(
            account_id=account.account_id,
            symbol="AAPL.US",
            quantity=Decimal("15"),
            available_quantity=Decimal("15"),
            avg_cost=Decimal("100"),
            market_price=Decimal("100"),
            market_value=Decimal("1500"),
            unrealized_pnl=Decimal("0"),
        )
    )
    config = RiskConfig(max_position_ratio_per_symbol=Decimal("0.2"))

    result = RiskEngine(config).check(session, _buy_intent(price="100", quantity="10"))

    assert result.approved is False
    assert result.reason_code == "MAX_POSITION_RATIO_EXCEEDED"


def test_rejects_when_account_loss_limit_is_reached() -> None:
    """账户已达到亏损阈值时，应停止新增交易。"""

    session = _session()
    _account(session, realized_pnl="-3000")

    result = RiskEngine().check(session, _buy_intent())

    assert result.approved is False
    assert result.reason_code == "DAILY_LOSS_LIMIT_EXCEEDED"


def test_market_order_uses_quote_for_cash_and_order_value_checks() -> None:
    """市价单应使用卖一价估值，不能因缺少限价而按零金额放行。"""

    session = _session()
    account = _account(session)
    account.cash_available = Decimal("500")
    intent = OrderIntent(
        account_id=account.account_id,
        symbol="AAPL.US",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
    )

    result = RiskEngine().check(
        session,
        intent,
        quote=MarketQuote(symbol="AAPL.US", last_price=Decimal("100"), ask_price=Decimal("101")),
        checked_at=datetime(2026, 6, 7, 12, tzinfo=UTC),
    )

    assert result.approved is False
    assert result.reason_code == "INSUFFICIENT_CASH"
    assert result.input_snapshot["estimated_value"] == "1010"
