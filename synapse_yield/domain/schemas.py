from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from synapse_yield.domain.enums import OrderSide, OrderType, TimeInForce


class OrderIntent(BaseModel):
    """策略或人工入口提交的下单意图。"""

    # 下单意图在创建后不应被业务流程原地修改，后续状态变化由订单状态机表达。
    model_config = ConfigDict(frozen=True)

    account_id: str  # 账户标识，用于区分资金、持仓和风控额度
    symbol: str  # 交易标的代码，例如股票代码或合约代码
    side: OrderSide  # 买卖方向
    order_type: OrderType  # 市价单或限价单
    quantity: Decimal = Field(gt=0)  # 委托数量，必须大于 0
    limit_price: Decimal | None = Field(default=None, gt=0)  # 限价单价格，市价单可为空
    time_in_force: TimeInForce = TimeInForce.DAY  # 订单有效期，默认当日有效
    source_signal_id: str | None = None  # 触发下单的信号 ID，便于追踪策略决策
    strategy_name: str | None = None  # 策略名称，便于审计和风控分组

    @model_validator(mode="after")
    def validate_order_price(self) -> "OrderIntent":
        """确保限价单在进入 Harness 前已经提供有效委托价格。"""

        # 市价单允许 limit_price 为空；限价单缺价无法执行撮合或资金预留。
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        return self


class RiskCheckResult(BaseModel):
    """风控引擎对一次下单意图的校验结果。"""

    # 风控结果应作为审计快照保存，避免后续流程意外改写判断依据。
    model_config = ConfigDict(frozen=True)

    approved: bool  # 是否允许订单继续提交
    reason_code: str  # 风控结果代码，用于日志、告警和前端展示分流
    message: str  # 面向操作人员的简短说明
    checked_rules: list[str]  # 本次实际执行过的风控规则列表
    input_snapshot: dict  # 风控输入快照，用于复盘当时的账户、行情和订单信息


class RiskConfig(BaseModel):
    """风控规则的可配置阈值。"""

    max_single_order_value: Decimal = Decimal("10000")  # 单笔订单最大名义金额
    max_position_ratio_per_symbol: Decimal = Decimal("0.2")  # 单一标的最大持仓占比
    max_total_position_ratio: Decimal = Decimal("0.8")  # 总持仓最大占账户权益比例
    max_daily_loss: Decimal = Decimal("3000")  # 单日最大允许亏损
    max_daily_order_count: int = 50  # 单日最大下单次数
    duplicate_order_cooldown_seconds: int = 60  # 重复订单冷却时间，降低误触发风险
    max_limit_price_deviation_ratio: Decimal = Decimal("0.03")  # 限价相对最新价最大偏离比例
    require_market_session: bool = True  # 是否要求只在交易时段内下单


class LocalSimConfig(BaseModel):
    """本地模拟盘的费用配置。"""

    # 按成交金额乘费率计算佣金；默认 0 便于无费用模拟。
    commission_rate: Decimal = Field(default=Decimal("0"), ge=0)
    # 费率佣金低于该值时收取最低佣金，默认同样为 0。
    minimum_commission: Decimal = Field(default=Decimal("0"), ge=0)


class MarketQuote(BaseModel):
    """风控和下单流程使用的行情快照。"""

    # 行情快照用于一次决策，不应在同一个对象上持续更新价格。
    model_config = ConfigDict(frozen=True)

    symbol: str  # 行情对应的交易标的
    last_price: Decimal = Field(gt=0)  # 最新成交价，必须大于 0
    bid_price: Decimal | None = Field(default=None, gt=0)  # 当前买一价，可为空
    ask_price: Decimal | None = Field(default=None, gt=0)  # 当前卖一价，可为空


class BrokerOrderResult(BaseModel):
    """Broker 或本地模拟盘返回的下单结果。"""

    # 下单结果是 Broker 对一次提交请求的确认，应作为不可变事件传递。
    model_config = ConfigDict(frozen=True)

    accepted: bool  # Broker 是否接收订单
    broker_order_id: str | None = None  # Broker 侧订单 ID，未接收时通常为空
    message: str  # Broker 返回的说明或错误信息
