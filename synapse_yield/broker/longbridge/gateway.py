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

    # 将内部订单枚举（OrderType/OrderSide/TimeInForce）转为长桥 SDK 枚举后提交订单，
    # 返回长桥分配的 broker_order_id。
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

    # 向长桥发送撤单请求，不等待最终确认，结果由推送或主动查询跟进。
    def cancel_order(self, broker_order_id: str) -> None:
        self.trade_context.cancel_order(broker_order_id)

    # 查询账户资金快照，解析 SDK 返回的多币种余额结构，提取与账户主货币匹配的可用资金。
    def account_balances(self) -> list[BrokerAccountSnapshot]:
        balances = self.trade_context.account_balance()
        return [self._account_snapshot(item) for item in balances]

    # 查询当前所有股票持仓，遍历 SDK 返回的 channel/positions 嵌套结构，展平为列表。
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

    # 按 broker_order_id 查询单笔订单的详细信息。
    def order_detail(self, broker_order_id: str) -> BrokerOrderSnapshot:
        return self._order_snapshot(self.trade_context.order_detail(broker_order_id))

    # 查询当日所有订单列表。
    def list_orders(self) -> list[BrokerOrderSnapshot]:
        return [self._order_snapshot(item) for item in self.trade_context.today_orders()]

    # 查询单只股票的实时行情快照，将 SDK 返回的字段统一映射到 MarketDataSnapshot。
    def quote(self, symbol: str) -> MarketDataSnapshot:
        response = self.quote_context.quote([symbol])
        item = response[0]
        timestamp = self._read(item, "timestamp", default=datetime.now(UTC))
        if not isinstance(timestamp, datetime):
            timestamp = datetime.now(UTC)
        return MarketDataSnapshot(
            symbol=str(self._read(item, "symbol", default=symbol)),  # 股票代码，如 AAPL.US
            timestamp=timestamp,                                       # 行情时间戳
            last_price=self._decimal(self._read(item, "last_done", "last_price")),  # 最新成交价
            open_price=self._optional_decimal(self._read(item, "open", default=None)),   # 今日开盘价
            high_price=self._optional_decimal(self._read(item, "high", default=None)),   # 今日最高价
            low_price=self._optional_decimal(self._read(item, "low", default=None)),     # 今日最低价
            volume=self._optional_decimal(self._read(item, "volume", default=None)),     # 成交量（股数）
            turnover=self._optional_decimal(self._read(item, "turnover", default=None)), # 成交额（金额）
            source="longbridge",                                       # 行情来源标识
        )

    # 拉取指定股票的历史 K 线数据。period 为周期（Day/Week 等），
    # count 为根数，adjust 为复权方式（NoAdjust/ForwardAdjust 等）。
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

    # 订阅长桥私有交易推送，每条推送先序列化为 dict 再交给 handler，
    # 屏蔽 SDK 对象类型，让上层代码无需依赖长桥 SDK。
    def subscribe_order_events(self, handler: Callable[[dict], None]) -> None:
        """订阅长桥私有交易主题，并把 SDK 对象转换为普通字典。"""

        self.trade_context.set_on_order_changed(lambda event: handler(self._to_dict(event)))
        private_topic = self._enum_member(self.sdk.TopicType, "Private")
        self.trade_context.subscribe([private_topic])

    # 订阅指定股票的实时行情推送，把 symbol 和行情字段合并成 dict 后交给 handler。
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

    # 将内部 OrderSide 枚举转为长桥 SDK 的 OrderSide 枚举。
    def _order_side(self, side: OrderSide):
        return self._enum_member(
            self.sdk.OrderSide,
            "Buy" if side == OrderSide.BUY else "Sell",
            side.value,
        )

    # 将内部 OrderType 枚举映射为长桥 SDK 的 OrderType 枚举（含 LIT/SLO 条件单）。
    def _order_type(self, order_type: OrderType):
        mapping = {
            OrderType.MARKET: ("MO", "Market"),
            OrderType.LIMIT: ("LO", "Limit"),
            OrderType.LIMIT_IF_TOUCHED: ("LIT",),   # 触价限价单（止盈）
            OrderType.STOP_LIMIT: ("SLO",),          # 止损限价单（止损）
        }
        candidates = mapping.get(order_type, ("LO", "Limit"))
        return self._enum_member(self.sdk.OrderType, *candidates)

    # 将内部 TimeInForce 枚举转为长桥 SDK 的 TimeInForceType 枚举。
    def _time_in_force(self, time_in_force: TimeInForce):
        candidates = (
            ("Day", "DAY")
            if time_in_force == TimeInForce.DAY
            else ("GoodTilCanceled", "GTC")
        )
        return self._enum_member(self.sdk.TimeInForceType, *candidates)

    # 将 SDK 返回的单条账户余额对象解析为 BrokerAccountSnapshot，
    # 从 cash_infos 中找到与账户主货币匹配的条目来提取可用资金。
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

    # 将 SDK 返回的单条订单对象解析为 BrokerOrderSnapshot，兼容多种字段名写法。
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

    # 从 SDK 对象或 dict 中按多个候选字段名依次读取值，全部缺失且无 default 时抛出异常。
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

    # 将任意 SDK 对象序列化为普通 dict，依次尝试 dict/model_dump/__dict__/公开字段提取。
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

    # 将单个值递归转为 JSON 可序列化类型（Decimal→str，datetime→ISO，枚举→str 等）。
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

    # 将任意值转为 Decimal，先转 str 再解析，避免浮点精度问题。
    @staticmethod
    def _decimal(value: Any) -> Decimal:
        return Decimal(str(value))

    # 可空版本的 _decimal，None 输入直接返回 None。
    @classmethod
    def _optional_decimal(cls, value: Any) -> Decimal | None:
        return None if value is None else cls._decimal(value)

    # 可空版本的 str 转换，None 输入直接返回 None。
    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return None if value is None else str(value)

    # 按候选名称列表依次查找长桥 SDK 枚举成员，全部找不到时抛出明确错误。
    # 用于兼容不同版本 SDK 对同一枚举的不同命名。
    @staticmethod
    def _enum_member(enum_type: Any, *candidates: str):
        for candidate in candidates:
            if hasattr(enum_type, candidate):
                return getattr(enum_type, candidate)
        raise RuntimeError(f"Longbridge SDK enum {enum_type} lacks members {candidates!r}")
