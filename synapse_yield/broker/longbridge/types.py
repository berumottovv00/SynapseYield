from dataclasses import dataclass, field
from decimal import Decimal

from synapse_yield.domain.enums import OrderSide, OrderType, TimeInForce


@dataclass(frozen=True)
class LongbridgeSubmitRequest:
    """传给长桥 SDK Gateway 的标准下单参数。"""

    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    time_in_force: TimeInForce
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None  # LIT（止盈）/ SLO（止损）触发价


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    """从长桥账户查询结果归一化出的资金快照。"""

    currency: str
    total_cash: Decimal
    available_cash: Decimal
    net_assets: Decimal
    raw_payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    """从长桥持仓查询结果归一化出的持仓快照。"""

    symbol: str
    quantity: Decimal
    available_quantity: Decimal
    average_cost: Decimal | None = None
    market_value: Decimal | None = None
    raw_payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    """从长桥订单查询结果归一化出的订单快照。"""

    broker_order_id: str
    symbol: str
    status: str
    side: str | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    average_fill_price: Decimal | None = None
    raw_payload: dict = field(default_factory=dict)
