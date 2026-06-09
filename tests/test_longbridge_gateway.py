from decimal import Decimal
from types import SimpleNamespace

import longbridge.openapi as sdk

from synapse_yield.broker.longbridge.gateway import LongbridgeSDKGateway
from synapse_yield.broker.longbridge.types import LongbridgeSubmitRequest
from synapse_yield.domain.enums import OrderSide, OrderType, TimeInForce


class FakeTradeContext:
    """记录 Gateway 传给官方 SDK Context 的参数。"""

    def __init__(self):
        self.submit_kwargs = None
        self.cancelled_order_id = None
        self.subscribed_topics = None
        self.order_callback = None

    def submit_order(self, **kwargs):
        self.submit_kwargs = kwargs
        return SimpleNamespace(order_id="lb_sdk_order")

    def cancel_order(self, order_id):
        self.cancelled_order_id = order_id

    def set_on_order_changed(self, callback):
        self.order_callback = callback

    def subscribe(self, topics):
        self.subscribed_topics = topics


class FakeQuoteContext:
    def __init__(self):
        self.quote_callback = None
        self.subscription = None

    def set_on_quote(self, callback):
        self.quote_callback = callback

    def subscribe(self, symbols, sub_types):
        self.subscription = (symbols, sub_types)


class RustLikeOrderEvent:
    """模拟没有 __dict__ 的官方 Rust 扩展响应对象。"""

    __slots__ = ("order_id", "status", "symbol")

    def __init__(self):
        self.order_id = "lb_sdk_order"
        self.status = sdk.OrderStatus.Filled
        self.symbol = "AAPL.US"


def _gateway() -> tuple[LongbridgeSDKGateway, FakeTradeContext, FakeQuoteContext]:
    trade_context = FakeTradeContext()
    quote_context = FakeQuoteContext()
    gateway = LongbridgeSDKGateway(
        sdk_config=object(),
        trade_context=trade_context,
        quote_context=quote_context,
    )
    return gateway, trade_context, quote_context


def test_gateway_maps_internal_order_to_official_sdk_enums() -> None:
    gateway, trade_context, _ = _gateway()

    broker_order_id = gateway.submit_order(
        LongbridgeSubmitRequest(
            client_order_id="client_1",
            symbol="AAPL.US",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("2"),
            limit_price=Decimal("100"),
            time_in_force=TimeInForce.DAY,
        )
    )

    assert broker_order_id == "lb_sdk_order"
    assert trade_context.submit_kwargs == {
        "symbol": "AAPL.US",
        "order_type": sdk.OrderType.LO,
        "side": sdk.OrderSide.Buy,
        "submitted_quantity": Decimal("2"),
        "time_in_force": sdk.TimeInForceType.Day,
        "remark": "client_1",
        "submitted_price": Decimal("100"),
    }


def test_gateway_uses_official_private_and_quote_subscription_signatures() -> None:
    gateway, trade_context, quote_context = _gateway()
    order_events: list[dict] = []
    quote_events: list[dict] = []

    gateway.subscribe_order_events(order_events.append)
    gateway.subscribe_quote_events(["AAPL.US"], quote_events.append)

    assert trade_context.subscribed_topics == [sdk.TopicType.Private]
    assert quote_context.subscription == (["AAPL.US"], [sdk.SubType.Quote])

    trade_context.order_callback(RustLikeOrderEvent())
    assert order_events[0]["order_id"] == "lb_sdk_order"
    assert "Filled" in order_events[0]["status"]
