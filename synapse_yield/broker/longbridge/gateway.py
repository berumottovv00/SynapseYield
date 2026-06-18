from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from synapse_yield.broker.longbridge.types import (
    BrokerAccountSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    LongbridgeSubmitRequest,
)
from synapse_yield.domain.enums import OrderSide, OrderType, TimeInForce
from synapse_yield.domain.schemas import MarketDataSnapshot


class LongbridgeGateway(Protocol):
    """LongbridgeBroker 使用的 SDK 隔离接口，测试时可注入 Fake。"""

    def submit_order(self, request: LongbridgeSubmitRequest) -> str: ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def account_balances(self) -> list[BrokerAccountSnapshot]: ...

    def positions(self) -> list[BrokerPositionSnapshot]: ...

    def order_detail(self, broker_order_id: str) -> BrokerOrderSnapshot: ...

    def list_orders(self) -> list[BrokerOrderSnapshot]: ...

    def quote(self, symbol: str) -> MarketDataSnapshot: ...

    def history_candlesticks(
        self,
        symbol: str,
        period: str = "Day",
        count: int = 60,
        adjust: str = "NoAdjust",
    ) -> list[Any]: ...

    def subscribe_order_events(self, handler: Callable[[dict], None]) -> None: ...

    def subscribe_quote_events(
        self,
        symbols: list[str],
        handler: Callable[[dict], None],
    ) -> None: ...


class LongbridgeSDKGateway:
    """官方 longbridge Python SDK 的薄封装。"""

    _PUBLIC_RESPONSE_FIELDS = (
        "order_id",
        "trade_id",
        "status",
        "symbol",
        "stock_name",
        "side",
        "order_type",
        "quantity",
        "submitted_quantity",
        "submitted_price",
        "executed_quantity",
        "executed_price",
        "currency",
        "total_cash",
        "available_cash",
        "frozen_cash",
        "net_assets",
        "cash_infos",
        "channels",
        "positions",
        "available_quantity",
        "cost_price",
        "market_value",
        "last_done",
        "open",
        "high",
        "low",
        "volume",
        "turnover",
        "timestamp",
        "submitted_at",
        "updated_at",
        "msg",
    )

    def __init__(
        self,
        *,
        sdk_config: Any | None = None,
        trade_context: Any | None = None,
        quote_context: Any | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        access_token: str | None = None,
    ):
        try:
            import longbridge.openapi as sdk
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Longbridge SDK is required. Install project dependencies first."
            ) from exc

        self.sdk = sdk
        if sdk_config is not None:
            config = sdk_config
        elif app_key and app_secret and access_token:
            config = sdk.Config.from_apikey(app_key, app_secret, access_token)
        else:
            config = sdk.Config.from_apikey_env()
        self.trade_context = trade_context or sdk.TradeContext(config)
        self.quote_context = quote_context or sdk.QuoteContext(config)

    def submit_order(self, request: LongbridgeSubmitRequest) -> str:
        """把内部订单枚举转换成 SDK 枚举后提交订单。"""

        kwargs: dict[str, Any] = {
            "symbol": request.symbol,
            "order_type": self._order_type(request.order_type),
            "side": self._order_side(request.side),
            "submitted_quantity": request.quantity,
            "time_in_force": self._time_in_force(request.time_in_force),
            "remark": request.client_order_id,
        }
        if request.limit_price is not None:
            kwargs["submitted_price"] = request.limit_price
        if request.trigger_price is not None:
            kwargs["trigger_price"] = request.trigger_price
        response = self.trade_context.submit_order(**kwargs)
        return str(self._read(response, "order_id"))

    def cancel_order(self, broker_order_id: str) -> None:
        self.trade_context.cancel_order(broker_order_id)

    def account_balances(self) -> list[BrokerAccountSnapshot]:
        balances = self.trade_context.account_balance()
        return [self._account_snapshot(item) for item in balances]

    def positions(self) -> list[BrokerPositionSnapshot]:
        response = self.trade_context.stock_positions()
        channels = self._read(response, "channels", default=response)
        positions: list[BrokerPositionSnapshot] = []
        for channel in channels:
            for item in self._read(channel, "positions", default=[]):
                raw = self._to_dict(item)
                positions.append(
                    BrokerPositionSnapshot(
                        symbol=str(self._read(item, "symbol")),
                        quantity=self._decimal(self._read(item, "quantity", default=0)),
                        available_quantity=self._decimal(
                            self._read(item, "available_quantity", default=0)
                        ),
                        average_cost=self._optional_decimal(
                            self._read(item, "cost_price", "average_cost", default=None)
                        ),
                        market_value=self._optional_decimal(
                            self._read(item, "market_value", default=None)
                        ),
                        raw_payload=raw,
                    )
                )
        return positions

    def order_detail(self, broker_order_id: str) -> BrokerOrderSnapshot:
        return self._order_snapshot(self.trade_context.order_detail(broker_order_id))

    def list_orders(self) -> list[BrokerOrderSnapshot]:
        return [self._order_snapshot(item) for item in self.trade_context.today_orders()]

    def quote(self, symbol: str) -> MarketDataSnapshot:
        response = self.quote_context.quote([symbol])
        item = response[0]
        timestamp = self._read(item, "timestamp", default=datetime.now(UTC))
        if not isinstance(timestamp, datetime):
            timestamp = datetime.now(UTC)
        return MarketDataSnapshot(
            symbol=str(self._read(item, "symbol", default=symbol)),
            timestamp=timestamp,
            last_price=self._decimal(self._read(item, "last_done", "last_price")),
            open_price=self._optional_decimal(self._read(item, "open", default=None)),
            high_price=self._optional_decimal(self._read(item, "high", default=None)),
            low_price=self._optional_decimal(self._read(item, "low", default=None)),
            volume=self._optional_decimal(self._read(item, "volume", default=None)),
            turnover=self._optional_decimal(self._read(item, "turnover", default=None)),
            source="longbridge",
        )

    def history_candlesticks(
        self,
        symbol: str,
        period: str = "Day",
        count: int = 60,
        adjust: str = "NoAdjust",
    ) -> list[Any]:
        """拉取历史 K 线，返回原始 SDK Candlestick 对象列表。"""
        period_enum = self._enum_member(self.sdk.Period, period)
        adjust_enum = self._enum_member(self.sdk.AdjustType, adjust)
        return list(
            self.quote_context.history_candlesticks_by_offset(
                symbol,
                period_enum,
                adjust_enum,
                forward=False,
                count=count,
            )
        )

    def subscribe_order_events(self, handler: Callable[[dict], None]) -> None:
        """订阅长桥私有交易主题，并把 SDK 对象转换为普通字典。"""

        self.trade_context.set_on_order_changed(lambda event: handler(self._to_dict(event)))
        private_topic = self._enum_member(self.sdk.TopicType, "Private")
        self.trade_context.subscribe([private_topic])

    def subscribe_quote_events(
        self,
        symbols: list[str],
        handler: Callable[[dict], None],
    ) -> None:
        self.quote_context.set_on_quote(
            lambda symbol, event: handler({"symbol": symbol, **self._to_dict(event)})
        )
        quote_type = self._enum_member(self.sdk.SubType, "Quote")
        self.quote_context.subscribe(symbols, [quote_type])

    def _order_side(self, side: OrderSide):
        return self._enum_member(
            self.sdk.OrderSide,
            "Buy" if side == OrderSide.BUY else "Sell",
            side.value,
        )

    def _order_type(self, order_type: OrderType):
        mapping = {
            OrderType.MARKET: ("MO", "Market"),
            OrderType.LIMIT: ("LO", "Limit"),
            OrderType.LIMIT_IF_TOUCHED: ("LIT",),   # 触价限价单（止盈）
            OrderType.STOP_LIMIT: ("SLO",),          # 止损限价单（止损）
        }
        candidates = mapping.get(order_type, ("LO", "Limit"))
        return self._enum_member(self.sdk.OrderType, *candidates)

    def _time_in_force(self, time_in_force: TimeInForce):
        candidates = (
            ("Day", "DAY")
            if time_in_force == TimeInForce.DAY
            else ("GoodTilCanceled", "GTC")
        )
        return self._enum_member(self.sdk.TimeInForceType, *candidates)

    def _account_snapshot(self, item: Any) -> BrokerAccountSnapshot:
        raw = self._to_dict(item)
        currency = str(self._read(item, "currency", default=""))
        cash_infos = self._read(item, "cash_infos", default=[])
        matching_cash_info = next(
            (
                cash_info
                for cash_info in cash_infos
                if str(self._read(cash_info, "currency", default="")) == currency
            ),
            None,
        )
        available_cash = (
            self._read(matching_cash_info, "available_cash", default=0)
            if matching_cash_info is not None
            else 0
        )
        return BrokerAccountSnapshot(
            currency=currency,
            total_cash=self._decimal(self._read(item, "total_cash", default=0)),
            available_cash=self._decimal(available_cash),
            net_assets=self._decimal(self._read(item, "net_assets", default=0)),
            raw_payload=raw,
        )

    def _order_snapshot(self, item: Any) -> BrokerOrderSnapshot:
        raw = self._to_dict(item)
        return BrokerOrderSnapshot(
            broker_order_id=str(self._read(item, "order_id")),
            symbol=str(self._read(item, "symbol", default="")),
            status=str(self._read(item, "status", default="UNKNOWN")),
            side=self._optional_string(self._read(item, "side", default=None)),
            quantity=self._optional_decimal(
                self._read(item, "quantity", "submitted_quantity", default=None)
            ),
            filled_quantity=self._optional_decimal(
                self._read(item, "executed_quantity", "filled_quantity", default=None)
            ),
            average_fill_price=self._optional_decimal(
                self._read(item, "executed_price", "average_fill_price", default=None)
            ),
            raw_payload=raw,
        )

    @staticmethod
    def _read(item: Any, *names: str, default: Any = ...):
        for name in names:
            if isinstance(item, dict) and name in item:
                return item[name]
            if hasattr(item, name):
                return getattr(item, name)
        if default is not ...:
            return default
        raise AttributeError(f"Missing fields {names!r} in Longbridge SDK response")

    @classmethod
    def _to_dict(cls, item: Any) -> dict:
        if isinstance(item, dict):
            return {key: cls._json_value(value) for key, value in item.items()}
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        values = getattr(item, "__dict__", None)
        if isinstance(values, Mapping):
            return {key: cls._json_value(value) for key, value in values.items()}
        extracted = {
            field: cls._json_value(getattr(item, field))
            for field in cls._PUBLIC_RESPONSE_FIELDS
            if hasattr(item, field)
        }
        if extracted:
            return extracted
        return {"value": str(item)}

    @classmethod
    def _json_value(cls, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {key: cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_value(item) for item in value]
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        return Decimal(str(value))

    @classmethod
    def _optional_decimal(cls, value: Any) -> Decimal | None:
        return None if value is None else cls._decimal(value)

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _enum_member(enum_type: Any, *candidates: str):
        for candidate in candidates:
            if hasattr(enum_type, candidate):
                return getattr(enum_type, candidate)
        raise RuntimeError(f"Longbridge SDK enum {enum_type} lacks members {candidates!r}")
