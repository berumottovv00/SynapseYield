from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synapse_yield.domain.enums import OrderSide, OrderStatus
from synapse_yield.domain.schemas import OrderIntent, RiskCheckResult, RiskConfig
from synapse_yield.storage.models import Account, Order, Position


class RiskEngine:
    """基础风控引擎，负责在订单提交前做同步规则校验。"""

    def __init__(self, config: RiskConfig | None = None):
        # 未传入配置时使用默认风控阈值，便于本地模拟和测试快速启动。
        self.config = config or RiskConfig()

    def check(self, session: Session, intent: OrderIntent) -> RiskCheckResult:
        """根据账户、订单意图和当前持仓/订单状态返回风控结论。"""

        # 风控先确认账户存在，再把订单意图保存为输入快照，方便后续审计和复盘。
        account = session.get(Account, intent.account_id)
        checked_rules = ["account_exists", "cash_available", "max_single_order_value"]
        input_snapshot = intent.model_dump(mode="json")

        if account is None:
            return RiskCheckResult(
                approved=False,
                reason_code="ACCOUNT_NOT_FOUND",
                message=f"Account {intent.account_id} does not exist",
                checked_rules=checked_rules,
                input_snapshot=input_snapshot,
            )

        # 估算订单名义金额；当前基础实现只对带 limit_price 的订单计算金额。
        estimated_value = self._estimate_order_value(intent)
        input_snapshot["estimated_value"] = str(estimated_value)
        input_snapshot["cash_available"] = str(account.cash_available)

        # 单笔订单金额不能超过配置阈值，避免一次下单暴露过大的风险敞口。
        if estimated_value > self.config.max_single_order_value:
            return RiskCheckResult(
                approved=False,
                reason_code="MAX_SINGLE_ORDER_VALUE_EXCEEDED",
                message="Order value exceeds max single order value",
                checked_rules=checked_rules,
                input_snapshot=input_snapshot,
            )

        # 买入订单必须有足够可用现金覆盖预估金额。
        if intent.side == OrderSide.BUY and account.cash_available < estimated_value:
            return RiskCheckResult(
                approved=False,
                reason_code="INSUFFICIENT_CASH",
                message="Available cash is not enough for this order",
                checked_rules=checked_rules,
                input_snapshot=input_snapshot,
            )

        # 卖出订单需要检查对应标的的可用持仓，避免超卖。
        if intent.side == OrderSide.SELL:
            checked_rules.append("position_available")
            position = session.scalar(
                select(Position).where(
                    Position.account_id == intent.account_id,
                    Position.symbol == intent.symbol,
                )
            )
            available_quantity = position.available_quantity if position else Decimal("0")
            input_snapshot["available_quantity"] = str(available_quantity)
            if available_quantity < intent.quantity:
                return RiskCheckResult(
                    approved=False,
                    reason_code="INSUFFICIENT_POSITION",
                    message="Available position is not enough for this sell order",
                    checked_rules=checked_rules,
                    input_snapshot=input_snapshot,
                )

        # 拦截同账户、同标的、同方向的相似未完成订单，降低重复提交风险。
        checked_rules.append("open_order_duplicate")
        duplicate_count = session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.account_id == intent.account_id,
                Order.symbol == intent.symbol,
                Order.side == intent.side,
                Order.status.in_(
                    [
                        OrderStatus.CREATED,
                        OrderStatus.RISK_APPROVED,
                        OrderStatus.SUBMITTING,
                        OrderStatus.SUBMITTED,
                        OrderStatus.PARTIALLY_FILLED,
                    ]
                ),
            )
        )
        input_snapshot["similar_open_orders"] = duplicate_count or 0
        if duplicate_count:
            return RiskCheckResult(
                approved=False,
                reason_code="DUPLICATE_OPEN_ORDER",
                message="A similar open order already exists",
                checked_rules=checked_rules,
                input_snapshot=input_snapshot,
            )

        # 所有基础规则通过后，订单可以进入后续状态机流程。
        return RiskCheckResult(
            approved=True,
            reason_code="OK",
            message="Risk checks passed",
            checked_rules=checked_rules,
            input_snapshot=input_snapshot,
        )

    @staticmethod
    def _estimate_order_value(intent: OrderIntent) -> Decimal:
        """估算订单名义金额；市价单暂时返回 0，等待行情定价模块补充。"""

        if intent.limit_price is None:
            return Decimal("0")
        return intent.quantity * intent.limit_price
