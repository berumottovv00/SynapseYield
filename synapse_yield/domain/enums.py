from enum import StrEnum


class OrderSide(StrEnum):
    """订单交易方向。"""

    BUY = "BUY"  # 买入，增加持仓并扣减现金
    SELL = "SELL"  # 卖出，减少持仓并增加现金


class OrderType(StrEnum):
    """订单类型。"""

    MARKET = "MARKET"  # 市价单，按当前可成交价格尽快成交
    LIMIT = "LIMIT"  # 限价单，只在达到指定价格条件时成交
    LIMIT_IF_TOUCHED = "LIMIT_IF_TOUCHED"  # 触价限价单（长桥 LIT），价格触及触发价后转为限价单，用于止盈
    STOP_LIMIT = "STOP_LIMIT"  # 止损限价单（长桥 SLO），价格跌至止损价后转为限价单，用于止损


class TimeInForce(StrEnum):
    """订单有效期类型。"""

    DAY = "DAY"  # 当日有效，交易日结束后未成交部分失效
    GTC = "GTC"  # 撤销前有效，订单持续有效直到成交或主动撤销


class OrderStatus(StrEnum):
    """订单生命周期状态，由 Harness 的订单状态机统一驱动。"""

    CREATED = "CREATED"  # 订单已在本地创建，尚未完成风控
    RISK_REJECTED = "RISK_REJECTED"  # 风控驳回，禁止提交给 Broker
    RISK_APPROVED = "RISK_APPROVED"  # 风控通过，允许进入提交流程
    SUBMITTING = "SUBMITTING"  # 正在提交给本地模拟盘或券商
    SUBMITTED = "SUBMITTED"  # Broker 已接收订单
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 订单部分成交，仍有剩余数量
    FILLED = "FILLED"  # 订单全部成交，生命周期正常结束
    CANCEL_PENDING = "CANCEL_PENDING"  # 撤单请求已发出，等待 Broker 确认
    CANCELLED = "CANCELLED"  # 订单已撤销，未成交部分不再有效
    FAILED = "FAILED"  # 订单明确失败，例如提交失败或 Broker 拒绝
    RECONCILING = "RECONCILING"  # 本地状态不确定，等待对账任务修复


class RiskDecisionStatus(StrEnum):
    """风控校验结果。"""

    APPROVED = "APPROVED"  # 风控通过，可以继续提交订单
    REJECTED = "REJECTED"  # 风控拒绝，订单不能提交
    REQUIRES_MANUAL_REVIEW = "REQUIRES_MANUAL_REVIEW"  # 需要人工确认后才能继续


class OutboxStatus(StrEnum):
    """Outbox 本地消息发布状态。"""

    PENDING = "PENDING"  # 事件已写入本地消息表，等待发布或消费
    PUBLISHED = "PUBLISHED"  # 事件已成功发布或处理
    FAILED = "FAILED"  # 事件发布或处理失败，等待重试或人工处理


class TradeAction(StrEnum):
    """大模型可提出的交易动作。"""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class LLMTradingRunStatus(StrEnum):
    """LLM 交易 Agent 一次运行的当前状态。"""

    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"
    NO_ACTION = "NO_ACTION"
    COMPLETED = "COMPLETED"
