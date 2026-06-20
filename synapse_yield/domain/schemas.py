from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from synapse_yield.domain.enums import (
    LLMTradingRunStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    TradeAction,
)


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
    trigger_price: Decimal | None = Field(default=None, gt=0)  # LIT/SLO 条件单触发价，普通订单为 None

    @model_validator(mode="after")
    def validate_order_price(self) -> "OrderIntent":
        """确保限价单和条件单在进入 Harness 前已经提供有效委托价格。"""

        conditional_types = {OrderType.LIMIT, OrderType.LIMIT_IF_TOUCHED, OrderType.STOP_LIMIT}
        if self.order_type in conditional_types and self.limit_price is None:
            raise ValueError(f"limit_price is required for {self.order_type} orders")
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

    max_single_order_value: Decimal = Decimal("1000000")  # 单笔订单最大名义金额
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


class MarketDataSnapshot(BaseModel):
    """策略层使用的标准化行情快照。"""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    last_price: Decimal = Field(gt=0)
    open_price: Decimal | None = Field(default=None, gt=0)
    high_price: Decimal | None = Field(default=None, gt=0)
    low_price: Decimal | None = Field(default=None, gt=0)
    volume: Decimal | None = Field(default=None, ge=0)
    turnover: Decimal | None = Field(default=None, ge=0)
    bid_price: Decimal | None = Field(default=None, gt=0)
    ask_price: Decimal | None = Field(default=None, gt=0)
    source: str


class BrokerOrderResult(BaseModel):
    """Broker 或本地模拟盘返回的下单结果。"""

    # 下单结果是 Broker 对一次提交请求的确认，应作为不可变事件传递。
    model_config = ConfigDict(frozen=True)

    accepted: bool  # Broker 是否接收订单
    broker_order_id: str | None = None  # Broker 侧订单 ID，未接收时通常为空
    message: str  # Broker 返回的说明或错误信息


class LLMTradeProposal(BaseModel):
    """大模型输出的结构化交易建议；它本身不具备下单权限。"""

    model_config = ConfigDict(frozen=True)

    action: TradeAction = Field(description="BUY、SELL 或 HOLD")
    symbol: str = Field(min_length=1, description="只能是输入行情中的证券代码")
    confidence: Decimal = Field(ge=0, le=1)
    quantity: Decimal = Field(ge=0)
    order_type: OrderType
    limit_price: Decimal | None
    take_profit_price: Decimal | None = Field(
        default=None,
        description="日内止盈价，BUY 时设置，价格触及时自动触发卖出",
    )
    stop_loss_price: Decimal | None = Field(
        default=None,
        description="日内止损价，BUY 时设置，价格跌至时自动触发卖出",
    )
    rationale: str = Field(min_length=1)
    risk_notes: list[str]

    @model_validator(mode="after")
    def validate_trade_parameters(self) -> "LLMTradeProposal":
        """约束 HOLD 和真实交易动作的数量及限价参数。"""

        if self.action == TradeAction.HOLD:
            if self.quantity != 0 or self.limit_price is not None:
                raise ValueError("HOLD requires quantity=0 and limit_price=null")
            return self
        if self.quantity <= 0:
            raise ValueError("BUY or SELL requires quantity > 0")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT proposal requires limit_price")
        return self


class HumanApprovalDecision(BaseModel):
    """人工对当前 LangGraph 线程中的交易建议作出的审批决定。"""

    model_config = ConfigDict(frozen=True)

    approved: bool
    reviewer: str = Field(min_length=1)
    reason: str = ""


class LLMTradingRunResult(BaseModel):
    """LLM 交易图启动、暂停或完成后返回给调用方的统一结果。"""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    trace_id: str
    status: LLMTradingRunStatus
    proposal: LLMTradeProposal
    approval_request: dict | None = None
    reviewer: str | None = None
    order_id: str | None = None
    risk_decision_id: str | None = None
    broker_order_id: str | None = None
    order_status: OrderStatus | None = None
    take_profit_order_id: str | None = None
    take_profit_broker_order_id: str | None = None
    stop_loss_order_id: str | None = None
    stop_loss_broker_order_id: str | None = None
    message: str


class StockPick(BaseModel):
    """LLM 从调研报告中挑选出的单支股票建议。"""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, description="证券代码，格式如 AAPL.US、00700.HK")
    action: TradeAction = Field(description="BUY 或 SELL，选股场景不输出 HOLD")
    confidence: Decimal = Field(
        ge=0,
        le=1,
        description="操作置信度，必须是 0.0-1.0 之间的小数，例如 0.85 表示高置信度，0.6 表示中等置信度",
    )
    quantity: Decimal = Field(
        ge=0,
        description="建议下单股数（整数），根据账户可用资金和报告中的价格信息计算，0 表示无法估算",
    )
    suggested_limit_price: Decimal | None = Field(
        default=None,
        description="建议限价，参考报告中提到的价格；市价单或价格不明时为 null",
    )
    take_profit_price: Decimal | None = Field(
        default=None,
        description="日内止盈价：BUY 操作时必填，当股价涨至此价触发卖出；参考报告中的压力位或目标价",
    )
    stop_loss_price: Decimal | None = Field(
        default=None,
        description="日内止损价：BUY 操作时必填，当股价跌至此价触发卖出；参考报告中的支撑位或风险价位",
    )
    rationale: str = Field(min_length=1, description="操作逻辑，需基于报告内容")
    key_catalysts: list[str] = Field(description="支撑本次操作的核心催化剂")
    risk_notes: list[str] = Field(default=[], description="主要风险提示，无风险时留空列表")

    @field_validator("suggested_limit_price", "take_profit_price", "stop_loss_price", mode="before")
    @classmethod
    def strip_price_formatting(cls, v: object) -> object:
        """去除 LLM 可能输出的货币符号、逗号等非数字字符，如 '$10.50' → '10.50'。"""
        if isinstance(v, str):
            cleaned = v.strip().lstrip("$¥€£").replace(",", "")
            return cleaned if cleaned else None
        return v


class StockSelectionResult(BaseModel):
    """LLM 对调研报告的选股输出，包含若干支最值得操作的股票。"""

    model_config = ConfigDict(frozen=True)

    picks: list[StockPick] = Field(description="按操作优先级降序排列的股票列表")
    market_summary: str = Field(default="", description="对当前市场环境的一句话总结")
