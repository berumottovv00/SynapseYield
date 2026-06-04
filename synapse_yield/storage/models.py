from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from synapse_yield.domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    OutboxStatus,
    RiskDecisionStatus,
    TimeInForce,
)
from synapse_yield.storage.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        comment="记录创建时间，统一使用 UTC 时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        comment="记录最后更新时间，统一使用 UTC 时间",
    )


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = {"comment": "账户资金表，记录账户现金、权益和盈亏汇总"}

    account_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="内部账户 ID，用于关联订单、持仓、资金流水",
    )
    base_currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="USD",
        comment="账户基础币种，例如 USD、HKD、CNY",
    )
    cash_available: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="账户可用现金，可用于新订单买入",
    )
    cash_frozen: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="账户冻结现金，通常用于已提交但未成交订单",
    )
    equity: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="账户总权益，通常等于现金加持仓市值",
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="已实现盈亏，来自已平仓或已卖出交易",
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="未实现盈亏，来自当前持仓浮动盈亏",
    )

    positions: Mapped[list["Position"]] = relationship(back_populates="account")


class Position(TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ux_positions_account_symbol", "account_id", "symbol", unique=True),
        {"comment": "持仓表，记录账户在每个证券上的当前持仓、成本和市值"},
    )

    position_id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
        comment="持仓记录自增主键",
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.account_id"),
        nullable=False,
        comment="所属账户 ID",
    )
    symbol: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="证券代码，内部统一格式如 AAPL.US、00700.HK",
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="当前总持仓数量",
    )
    available_quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="当前可卖或可用持仓数量，扣除冻结部分",
    )
    avg_cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="持仓平均成本价",
    )
    market_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="最近一次更新的市场价格",
    )
    market_value: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="持仓市值，通常等于 quantity * market_price",
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="该持仓的未实现盈亏",
    )

    account: Mapped[Account] = relationship(back_populates="positions")


class StrategySignal(TimestampMixin, Base):
    __tablename__ = "strategy_signals"
    __table_args__ = {"comment": "策略信号表，记录策略根据行情生成的买卖建议和原始输出"}

    signal_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="策略信号 ID，用于关联后续风控和订单",
    )
    trace_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="链路追踪 ID，贯穿行情、策略、风控、下单和审计",
    )
    strategy_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="产生该信号的策略名称",
    )
    strategy_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="v1",
        comment="策略版本，用于回放和区分策略迭代",
    )
    symbol: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="信号对应的证券代码",
    )
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide),
        nullable=False,
        comment="策略建议方向，BUY 表示买入，SELL 表示卖出",
    )
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        default=0,
        comment="策略置信度，范围通常为 0 到 1",
    )
    target_quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        comment="策略建议交易数量",
    )
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType),
        nullable=False,
        comment="策略建议订单类型，例如 MARKET 或 LIMIT",
    )
    limit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6),
        comment="策略建议限价，市价单可为空",
    )
    reason: Mapped[str | None] = mapped_column(
        String(255),
        comment="策略生成该信号的简短原因",
    )
    raw_payload: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="策略原始输出快照，用于审计、回放和排查",
    )


class RiskDecision(TimestampMixin, Base):
    __tablename__ = "risk_decisions"
    __table_args__ = {"comment": "风控决策表，记录每次订单意图的风控校验结果和原因"}

    risk_decision_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="风控决策 ID，用于关联订单和审计日志",
    )
    trace_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="链路追踪 ID",
    )
    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_signals.signal_id"),
        comment="关联的策略信号 ID；手工订单或系统订单可为空",
    )
    decision: Mapped[RiskDecisionStatus] = mapped_column(
        Enum(RiskDecisionStatus),
        nullable=False,
        comment="风控结果：通过、拒绝或需要人工确认",
    )
    reason_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="机器可读的风控原因码，例如 OK、INSUFFICIENT_CASH",
    )
    message: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="面向日志和人工排查的风控说明",
    )
    checked_rules: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="本次风控实际检查过的规则列表",
    )
    input_snapshot: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="风控输入快照，包括账户、持仓和订单意图",
    )


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = {"comment": "订单主表，记录订单生命周期、状态机状态、幂等键和券商订单映射"}

    order_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="内部订单 ID，系统内订单主键",
    )
    client_order_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="客户端订单 ID，提交给券商时用于幂等和追踪",
    )
    broker_order_id: Mapped[str | None] = mapped_column(
        String(128),
        unique=True,
        comment="券商返回的订单 ID，本地模拟盘或长桥生成",
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.account_id"),
        nullable=False,
        comment="下单账户 ID",
    )
    symbol: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="订单对应的证券代码",
    )
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide),
        nullable=False,
        comment="订单方向，BUY 买入，SELL 卖出",
    )
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType),
        nullable=False,
        comment="订单类型，例如 MARKET 市价单或 LIMIT 限价单",
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus),
        nullable=False,
        comment="订单当前状态，由订单状态机驱动",
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        comment="订单委托数量",
    )
    filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="累计已成交数量",
    )
    limit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6),
        comment="限价单委托价格，市价单为空",
    )
    avg_fill_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6),
        comment="平均成交价格，未成交时为空",
    )
    time_in_force: Mapped[TimeInForce] = mapped_column(
        Enum(TimeInForce),
        nullable=False,
        comment="订单有效期，例如 DAY 当日有效或 GTC 撤销前有效",
    )
    source_signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_signals.signal_id"),
        comment="产生该订单的策略信号 ID",
    )
    risk_decision_id: Mapped[str | None] = mapped_column(
        ForeignKey("risk_decisions.risk_decision_id"),
        comment="该订单对应的风控决策 ID",
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
        comment="订单幂等键，防止同一信号重复生成有效订单",
    )
    raw_request: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="订单创建时的原始请求快照",
    )

    fills: Mapped[list["Fill"]] = relationship(back_populates="order")


class Fill(TimestampMixin, Base):
    __tablename__ = "fills"
    __table_args__ = {"comment": "成交明细表，记录订单的每笔成交、价格、数量和手续费"}

    fill_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="内部成交 ID，系统内成交主键",
    )
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id"),
        nullable=False,
        comment="所属订单 ID",
    )
    broker_fill_id: Mapped[str | None] = mapped_column(
        String(128),
        unique=True,
        comment="券商返回的成交 ID，用于成交回报去重",
    )
    symbol: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="成交对应的证券代码",
    )
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide),
        nullable=False,
        comment="成交方向，继承订单方向",
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        comment="本笔成交数量",
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        comment="本笔成交价格",
    )
    commission: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=0,
        comment="本笔成交手续费或佣金",
    )
    fill_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="成交发生时间，来自模拟盘或券商回报",
    )

    order: Mapped[Order] = relationship(back_populates="fills")


class CashLedger(TimestampMixin, Base):
    __tablename__ = "cash_ledger"
    __table_args__ = {"comment": "资金流水表，记录买入、卖出、手续费等导致的账户现金变化"}

    ledger_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="资金流水 ID",
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.account_id"),
        nullable=False,
        comment="资金流水所属账户 ID",
    )
    order_id: Mapped[str | None] = mapped_column(
        ForeignKey("orders.order_id"),
        comment="关联订单 ID；非交易类资金变动可为空",
    )
    fill_id: Mapped[str | None] = mapped_column(
        ForeignKey("fills.fill_id"),
        comment="关联成交 ID；非成交类资金变动可为空",
    )
    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="资金事件类型，例如 BUY_FILL、SELL_FILL、COMMISSION",
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        comment="本次资金变动金额，收入为正，支出为负",
    )
    currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="资金变动币种",
    )
    balance_after: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        comment="本次资金变动后的账户可用余额",
    )


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = {"comment": "审计日志表，记录 Harness 全链路动作、输入输出快照和状态迁移"}

    audit_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="审计日志 ID",
    )
    trace_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="链路追踪 ID，用于串起一次完整交易流程",
    )
    actor_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="动作发起方类型，例如 HARNESS、AGENT、SKILL、BROKER",
    )
    actor_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="动作发起方名称，例如 OrderService、RiskAgent",
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="审计事件类型，例如 ORDER_CREATED、RISK_CHECK_COMPLETED",
    )
    input_snapshot: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="动作执行前的输入快照",
    )
    output_snapshot: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="动作执行后的输出快照",
    )
    previous_state: Mapped[str | None] = mapped_column(
        String(64),
        comment="动作发生前的状态，适用于订单状态迁移",
    )
    next_state: Mapped[str | None] = mapped_column(
        String(64),
        comment="动作发生后的状态，适用于订单状态迁移",
    )


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = {"comment": "本地消息表，用于可靠发布领域事件和实现最终一致性"}

    event_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="Outbox 事件 ID",
    )
    trace_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="链路追踪 ID",
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="事件类型，例如 ORDER_READY_TO_SUBMIT、ORDER_FILLED",
    )
    aggregate_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="聚合根类型，例如 ORDER、ACCOUNT、POSITION",
    )
    aggregate_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="聚合根 ID，例如订单 ID",
    )
    payload: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="事件载荷，供异步执行、通知或后续 Kafka 发布使用",
    )
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus),
        nullable=False,
        comment="Outbox 事件状态：待发布、已发布或失败",
    )
    attempts: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        comment="事件发布或处理尝试次数",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        comment="最近一次处理失败的错误信息",
    )


class BrokerEvent(TimestampMixin, Base):
    __tablename__ = "broker_events"
    __table_args__ = {"comment": "券商事件表，保存本地模拟盘或长桥推送的原始订单与成交事件"}

    broker_event_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="券商事件 ID，保存原始回报或推送事件",
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
        comment="链路追踪 ID；外部推送事件可能无法立即关联，可为空",
    )
    broker_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="券商或数据源名称，例如 local_sim、longbridge",
    )
    broker_order_id: Mapped[str | None] = mapped_column(
        String(128),
        index=True,
        comment="券商订单 ID，用于关联本地订单",
    )
    broker_fill_id: Mapped[str | None] = mapped_column(
        String(128),
        index=True,
        comment="券商成交 ID，用于成交事件去重",
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="券商事件类型，例如 ORDER_UPDATE、FILL、CANCEL",
    )
    raw_payload: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="券商原始事件内容，便于审计、回放和故障排查",
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="事件被系统成功处理的时间；为空表示尚未处理",
    )


class ReconcileTask(TimestampMixin, Base):
    __tablename__ = "reconcile_tasks"
    __table_args__ = {"comment": "对账任务表，记录本地状态与券商状态不一致时的修复任务"}

    task_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="对账任务 ID",
    )
    trace_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="链路追踪 ID",
    )
    task_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="对账任务类型，例如 ORDER_RECONCILE、POSITION_RECONCILE",
    )
    target_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="对账目标类型，例如 ORDER、POSITION、ACCOUNT",
    )
    target_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="对账目标 ID，例如订单 ID 或券商订单 ID",
    )
    status: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="PENDING",
        comment="对账任务状态，例如 PENDING、DONE、FAILED、MANUAL_REQUIRED",
    )
    reason: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="创建对账任务的原因",
    )
    result_payload: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="对账处理结果或人工处理记录",
    )


class MarketSnapshot(TimestampMixin, Base):
    __tablename__ = "market_snapshots"
    __table_args__ = {"comment": "行情快照表，记录策略计算和回放所需的标准化行情数据"}

    snapshot_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="行情快照 ID",
    )
    trace_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="链路追踪 ID，用于关联行情触发的后续策略和订单",
    )
    symbol: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="行情对应的证券代码",
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="行情时间戳，来自数据源或采集时间",
    )
    last_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        comment="最新成交价或最新报价",
    )
    open_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6),
        comment="当前周期或交易日开盘价",
    )
    high_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6),
        comment="当前周期或交易日最高价",
    )
    low_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6),
        comment="当前周期或交易日最低价",
    )
    volume: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6),
        comment="成交量",
    )
    turnover: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6),
        comment="成交额",
    )
    bid_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6),
        comment="买一价或当前最优买价",
    )
    ask_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6),
        comment="卖一价或当前最优卖价",
    )
    source: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="行情来源，例如 local_sim、longbridge",
    )
