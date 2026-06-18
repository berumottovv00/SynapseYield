from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synapse_yield.domain.enums import OrderSide, OrderStatus
from synapse_yield.domain.schemas import MarketQuote, OrderIntent, RiskCheckResult, RiskConfig
from synapse_yield.storage.models import Account, Order, Position


class RiskEngine:
    """基础风控引擎，负责在订单提交前做同步规则校验。"""

    def __init__(self, config: RiskConfig | None = None):
        # 未传入配置时使用默认风控阈值，便于本地模拟和测试快速启动。
        self.config = config or RiskConfig()

    def check(
        self,
        session: Session,
        intent: OrderIntent,
        exclude_order_id: str | None = None,
        quote: MarketQuote | None = None,
        market_is_open: bool | None = None,
        checked_at: datetime | None = None,
    ) -> RiskCheckResult:
        """根据账户、订单意图和当前持仓/订单状态返回风控结论。"""

        # checked_rules 和 input_snapshot 会随风控结果持久化，供审计和回放使用。
        account = session.get(Account, intent.account_id)
        checked_rules = ["account_exists"]
        input_snapshot = intent.model_dump(mode="json")
        now = checked_at or datetime.now(UTC)

        # 账户不存在时，后续资金、持仓和仓位规则都没有可靠数据来源。
        if account is None:
            return self._rejected(
                "ACCOUNT_NOT_FOUND",
                f"Account {intent.account_id} does not exist",
                checked_rules,
                input_snapshot,
            )

        # 防止调用方误把其他标的行情用于订单估值和价格偏离检查。
        if quote is not None and quote.symbol != intent.symbol:
            checked_rules.append("quote_symbol")
            return self._rejected(
                "QUOTE_SYMBOL_MISMATCH",
                "Quote symbol does not match order intent",
                checked_rules,
                input_snapshot,
            )

        # 交易时段由外部交易日历判断并显式传入，风控引擎只消费确定性结果。
        if self.config.require_market_session and market_is_open is False:
            checked_rules.append("market_session")
            return self._rejected(
                "MARKET_CLOSED",
                "Orders are not allowed outside the configured market session",
                checked_rules,
                input_snapshot,
            )
        if market_is_open is not None:
            checked_rules.append("market_session")

        # 限价单按委托价、市价单按盘口价估算名义金额。
        estimated_value = self._estimate_order_value(intent, quote)
        input_snapshot["estimated_value"] = str(estimated_value)
        input_snapshot["cash_available"] = str(account.cash_available)

        # 限制单笔风险暴露，避免一次异常信号消耗过多资金。
        checked_rules.append("max_single_order_value")
        if estimated_value > self.config.max_single_order_value:
            return self._rejected(
                "MAX_SINGLE_ORDER_VALUE_EXCEEDED",
                "Order value exceeds max single order value",
                checked_rules,
                input_snapshot,
            )

        # 买单必须能够由当前可用现金覆盖；卖单在后面检查可卖数量。
        checked_rules.append("cash_available")
        if intent.side == OrderSide.BUY and account.cash_available < estimated_value:
            return self._rejected(
                "INSUFFICIENT_CASH",
                "Available cash is not enough for this order",
                checked_rules,
                input_snapshot,
            )

        if intent.side == OrderSide.SELL:
            # 使用 available_quantity 而非总持仓，已被其他卖单冻结的数量不能重复使用。
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
                return self._rejected(
                    "INSUFFICIENT_POSITION",
                    "Available position is not enough for this sell order",
                    checked_rules,
                    input_snapshot,
                )

        # 当账户亏损达到阈值时停止新增交易，避免亏损继续扩大。
        checked_rules.append("daily_loss_limit")
        input_snapshot["realized_pnl"] = str(account.realized_pnl)
        if account.realized_pnl <= -self.config.max_daily_loss:
            return self._rejected(
                "DAILY_LOSS_LIMIT_EXCEEDED",
                "Account loss has reached the configured daily limit",
                checked_rules,
                input_snapshot,
            )

        # 每日订单计数统一按 UTC 日期统计，checked_at 可在回放测试中固定时间。
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        checked_rules.append("daily_order_count")
        # 当前正在接受风控的订单已经落库，因此必须从统计中排除自身。
        daily_order_count = session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.account_id == intent.account_id,
                Order.created_at >= day_start,
                Order.order_id != exclude_order_id if exclude_order_id else True,
            )
        ) or 0
        input_snapshot["daily_order_count"] = daily_order_count
        if daily_order_count >= self.config.max_daily_order_count:
            return self._rejected(
                "MAX_DAILY_ORDER_COUNT_EXCEEDED",
                "Account has reached the maximum daily order count",
                checked_rules,
                input_snapshot,
            )

        # 仓位比例只限制新增买入；卖出会降低风险敞口，无需执行这两项拦截。
        # 有行情时限制限价偏离，拦截价格单位错误或明显异常的委托。
        if intent.limit_price is not None and quote is not None:
            checked_rules.append("limit_price_deviation")
            deviation = abs(intent.limit_price - quote.last_price) / quote.last_price
            input_snapshot["limit_price_deviation_ratio"] = str(deviation)
            if deviation > self.config.max_limit_price_deviation_ratio:
                return self._rejected(
                    "LIMIT_PRICE_DEVIATION_EXCEEDED",
                    "Limit price deviates too far from the latest market price",
                    checked_rules,
                    input_snapshot,
                )

        # 未完成的同账户、同标的、同方向订单会造成重复风险暴露。
        checked_rules.append("open_order_duplicate")
        duplicate_filters = [
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
        ]
        # OrderService 会先创建当前订单再执行风控，所以查询必须排除当前订单。
        if exclude_order_id is not None:
            duplicate_filters.append(Order.order_id != exclude_order_id)

        duplicate_count = session.scalar(
            select(func.count()).select_from(Order).where(*duplicate_filters)
        )
        input_snapshot["similar_open_orders"] = duplicate_count or 0
        if duplicate_count:
            return self._rejected(
                "DUPLICATE_OPEN_ORDER",
                "A similar open order already exists",
                checked_rules,
                input_snapshot,
            )

        # 即使上一张订单已经结束，冷却期内也禁止快速重复触发相似订单。
        checked_rules.append("duplicate_order_cooldown")
        cooldown_start = now - timedelta(seconds=self.config.duplicate_order_cooldown_seconds)
        recent_duplicate_filters = [
            Order.account_id == intent.account_id,
            Order.symbol == intent.symbol,
            Order.side == intent.side,
            Order.created_at >= cooldown_start,
        ]
        if exclude_order_id is not None:
            recent_duplicate_filters.append(Order.order_id != exclude_order_id)
        recent_duplicate_count = session.scalar(
            select(func.count()).select_from(Order).where(*recent_duplicate_filters)
        ) or 0
        input_snapshot["recent_similar_orders"] = recent_duplicate_count
        if recent_duplicate_count:
            return self._rejected(
                "DUPLICATE_ORDER_COOLDOWN",
                "A similar order was created within the configured cooldown period",
                checked_rules,
                input_snapshot,
            )

        # 运行到这里表示所有已启用的同步规则均通过。
        return RiskCheckResult(
            approved=True,
            reason_code="OK",
            message="Risk checks passed",
            checked_rules=checked_rules,
            input_snapshot=input_snapshot,
        )

    @staticmethod
    def _estimate_order_value(
        intent: OrderIntent,
        quote: MarketQuote | None = None,
    ) -> Decimal:
        """使用限价或最新行情估算订单名义金额。"""

        # 市价买单按卖一估值，卖单按买一估值，盘口缺失时使用最新价。
        price = intent.limit_price
        if price is None and quote is not None:
            if intent.side == OrderSide.BUY:
                price = quote.ask_price or quote.last_price
            else:
                price = quote.bid_price or quote.last_price
        return intent.quantity * price if price is not None else Decimal("0")

    @staticmethod
    def _rejected(
        reason_code: str,
        message: str,
        checked_rules: list[str],
        input_snapshot: dict,
    ) -> RiskCheckResult:
        """统一构造风控拒绝结果，保证返回结构和审计字段一致。"""

        return RiskCheckResult(
            approved=False,
            reason_code=reason_code,
            message=message,
            checked_rules=checked_rules,
            input_snapshot=input_snapshot,
        )
