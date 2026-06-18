from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse_yield.domain.enums import OrderSide, OrderStatus, OrderType
from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import BrokerOrderResult, LocalSimConfig, MarketQuote
from synapse_yield.harness.order_service import OrderService
from synapse_yield.storage.models import (
    Account,
    BrokerEvent,
    CashLedger,
    Fill,
    Order,
    Position,
)


class LocalSimBroker:
    """支持资源预留、行情撮合、撤单和账本更新的本地模拟盘。"""

    broker_name = "local_sim"

    def __init__(
        self,
        order_service: OrderService | None = None,
        config: LocalSimConfig | None = None,
    ):
        # 订单服务负责统一推进状态机；模拟盘配置负责佣金等可变交易参数。
        self.order_service = order_service or OrderService()
        self.config = config or LocalSimConfig()

    def submit_order(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        quote: MarketQuote,
    ) -> BrokerOrderResult:
        """接收风控通过的订单，预留资金或持仓，并尝试立即成交。"""

        # Broker 入口再次校验状态，防止调用方绕过 Harness 的风控流程。
        if order.status != OrderStatus.RISK_APPROVED:
            return BrokerOrderResult(
                accepted=False,
                message=f"Order status {order.status} is not submittable",
            )
        # 行情和订单标的不一致时不能撮合，否则可能按错误证券价格成交。
        if quote.symbol != order.symbol:
            self.order_service.transition_order(
                session, trace_id, order, OrderStatus.FAILED, "LOCAL_SIM_SYMBOL_MISMATCH"
            )
            return BrokerOrderResult(accepted=False, message="Quote symbol does not match order")

        # 接单前先冻结买入资金或卖出持仓，避免多个未成交订单重复占用资源。
        error = self._reserve_resources(session, trace_id, order, quote)
        if error is not None:
            self.order_service.transition_order(
                session, trace_id, order, OrderStatus.FAILED, "LOCAL_SIM_RESERVATION_FAILED"
            )
            return BrokerOrderResult(accepted=False, message=error)

        # 模拟券商接单过程：先进入提交中，再生成 Broker 订单号并确认已提交。
        broker_order_id = new_id("ls_order")
        self.order_service.transition_order(
            session, trace_id, order, OrderStatus.SUBMITTING, "LOCAL_SIM_ORDER_SUBMITTING"
        )
        order.broker_order_id = broker_order_id
        self.order_service.transition_order(
            session, trace_id, order, OrderStatus.SUBMITTED, "LOCAL_SIM_ORDER_SUBMITTED"
        )
        # 保存模拟盘原始接单事件，后续可用于回放和对账。
        self._record_broker_event(
            session,
            trace_id,
            order,
            event_type="ORDER_ACCEPTED",
            raw_payload={"client_order_id": order.client_order_id},
        )

        # 市价单通常立即成交；可成交限价单也会在提交后立即落账。
        fill_price = self._fill_price(order, quote)
        if fill_price is not None:
            self._apply_fill(session, trace_id, order, fill_price)

        return BrokerOrderResult(
            accepted=True,
            broker_order_id=broker_order_id,
            message="Order accepted by local simulator",
        )

    def process_quote(
        self,
        session: Session,
        trace_id: str,
        quote: MarketQuote,
    ) -> list[str]:
        """用新行情撮合此前未成交的订单，返回本次成交的订单 ID。"""

        # 只扫描同标的、已被 Broker 接收且尚未成交的订单。
        orders = session.scalars(
            select(Order).where(
                Order.symbol == quote.symbol,
                Order.status == OrderStatus.SUBMITTED,
            )
        ).all()
        filled_order_ids: list[str] = []
        # 每次新行情到达时逐单重新判断，未达到限价条件的订单继续等待。
        for order in orders:
            fill_price = self._fill_price(order, quote)
            if fill_price is None:
                continue
            self._apply_fill(session, trace_id, order, fill_price)
            filled_order_ids.append(order.order_id)
        return filled_order_ids

    def cancel_order(
        self,
        session: Session,
        trace_id: str,
        order: Order,
    ) -> BrokerOrderResult:
        """撤销未成交订单，并释放此前冻结的现金或持仓。"""

        # 当前版本只支持撤销完全未成交的 SUBMITTED 订单。
        if order.status != OrderStatus.SUBMITTED:
            return BrokerOrderResult(
                accepted=False,
                broker_order_id=order.broker_order_id,
                message=f"Order status {order.status} is not cancellable",
            )

        # 先进入撤单处理中，释放资源后再确认撤单完成，保持状态与账本顺序一致。
        self.order_service.transition_order(
            session, trace_id, order, OrderStatus.CANCEL_PENDING, "LOCAL_SIM_CANCEL_PENDING"
        )
        self._release_resources(session, trace_id, order)
        self.order_service.transition_order(
            session, trace_id, order, OrderStatus.CANCELLED, "LOCAL_SIM_ORDER_CANCELLED"
        )
        # BrokerEvent 保存模拟券商回报，OutboxEvent 用于通知系统下游。
        self._record_broker_event(
            session,
            trace_id,
            order,
            event_type="ORDER_CANCELLED",
            raw_payload={"order_id": order.order_id},
        )
        self.order_service.enqueue_event(
            session,
            trace_id,
            event_type="ORDER_CANCELLED",
            aggregate_type="ORDER",
            aggregate_id=order.order_id,
            payload={"order_id": order.order_id},
        )
        return BrokerOrderResult(
            accepted=True,
            broker_order_id=order.broker_order_id,
            message="Order cancelled by local simulator",
        )

    def _reserve_resources(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        quote: MarketQuote,
    ) -> str | None:
        """冻结订单可能使用的现金或持仓；成功返回 None，失败返回原因。"""

        account = session.get(Account, order.account_id)
        if account is None:
            return f"Account {order.account_id} does not exist"

        if order.side == OrderSide.BUY:
            # 买单按限价或当前卖价连同佣金预冻结，成交价格改善后退回差额。
            reserved_cash = self._reserved_cash(order, quote)
            if account.cash_available < reserved_cash:
                return "Available cash is not enough to reserve this order"
            account.cash_available -= reserved_cash
            account.cash_frozen += reserved_cash
            output = {
                "cash_available": str(account.cash_available),
                "cash_frozen": str(account.cash_frozen),
            }
        elif self._is_bracket_order(order):
            # 止盈/止损子单：配对买单尚未成交，持仓还不存在，跳过持仓预留。
            output = {"bracket": order.order_type.value}
        else:
            # 普通卖单从可用持仓中扣除委托数量，总持仓暂时不变。
            position = self._find_position(session, order.account_id, order.symbol)
            if position is None or position.available_quantity < order.quantity:
                return "Available position is not enough to reserve this order"
            position.available_quantity -= order.quantity
            output = {"available_quantity": str(position.available_quantity)}

        # 资源变化不属于订单状态迁移，单独写入审计事件。
        self.order_service.record_event(
            session,
            trace_id,
            "LOCAL_SIM_RESOURCES_RESERVED",
            {"order_id": order.order_id, "side": order.side.value},
            output,
        )
        return None

    @staticmethod
    def _is_bracket_order(order: Order) -> bool:
        """止盈（LIT）/止损（STOP_LIMIT）卖单在主买单成交前提交，需跳过持仓预留检查。"""
        return order.side == OrderSide.SELL and order.order_type in {
            OrderType.LIMIT_IF_TOUCHED,
            OrderType.STOP_LIMIT,
        }

    def _release_resources(self, session: Session, trace_id: str, order: Order) -> None:
        """释放撤单后不再占用的冻结现金或可卖持仓。"""

        account = session.get(Account, order.account_id)
        if account is None:
            raise ValueError(f"Account {order.account_id} does not exist")

        if order.side == OrderSide.BUY:
            # 未成交买单撤销后，原冻结金额全部恢复为可用现金。
            reserved_cash = self._reserved_cash(order)
            account.cash_frozen -= reserved_cash
            account.cash_available += reserved_cash
            output = {
                "cash_available": str(account.cash_available),
                "cash_frozen": str(account.cash_frozen),
            }
        elif self._is_bracket_order(order):
            # 止盈/止损子单未曾预留持仓，无需释放。
            output = {"bracket": order.order_type.value}
        else:
            # 普通卖单释放尚未成交的数量。
            position = self._find_position(session, order.account_id, order.symbol)
            if position is None:
                raise ValueError(f"Position {order.account_id}/{order.symbol} does not exist")
            position.available_quantity += order.quantity - order.filled_quantity
            output = {"available_quantity": str(position.available_quantity)}

        self.order_service.record_event(
            session,
            trace_id,
            "LOCAL_SIM_RESOURCES_RELEASED",
            {"order_id": order.order_id},
            output,
        )

    def _fill_price(self, order: Order, quote: MarketQuote) -> Decimal | None:
        """根据订单类型和买卖盘价格返回成交价；None 表示当前不能成交。"""

        # 市价买单取卖一，市价卖单取买一；盘口缺失时退回最新价。
        if order.order_type == OrderType.MARKET:
            if order.side == OrderSide.BUY:
                return quote.ask_price or quote.last_price
            return quote.bid_price or quote.last_price

        if order.limit_price is None:
            return None

        # 止盈触价限价卖单（LIT）：价格≥触发价时按 limit_price 成交（模拟：用 limit_price 作触发价）
        if order.order_type == OrderType.LIMIT_IF_TOUCHED and order.side == OrderSide.SELL:
            bid_price = quote.bid_price or quote.last_price
            return bid_price if bid_price >= order.limit_price else None

        # 止损限价卖单（STOP_LIMIT）：价格≤触发价时以 limit_price 成交
        if order.order_type == OrderType.STOP_LIMIT and order.side == OrderSide.SELL:
            bid_price = quote.bid_price or quote.last_price
            return bid_price if bid_price <= order.limit_price else None

        # 普通买入限价：bid_price 覆盖卖一时成交
        if order.side == OrderSide.BUY:
            ask_price = quote.ask_price or quote.last_price
            return ask_price if order.limit_price >= ask_price else None

        # 普通卖出限价：卖出价≥买一时成交
        bid_price = quote.bid_price or quote.last_price
        return bid_price if order.limit_price <= bid_price else None

    def _apply_fill(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        fill_price: Decimal,
    ) -> None:
        account = session.get(Account, order.account_id)
        if account is None:
            raise ValueError(f"Account {order.account_id} does not exist")

        # 当前第二阶段采用整单一次成交，成交金额和佣金均按全部数量计算。
        commission = self._commission(order.quantity * fill_price)
        fill = Fill(
            fill_id=new_id("fill"),
            order_id=order.order_id,
            broker_fill_id=new_id("ls_fill"),
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=commission,
            fill_time=datetime.now(UTC),
        )
        session.add(fill)

        # 先准备成交金额和持仓对象，再按买卖方向执行不同账本动作。
        gross_amount = order.quantity * fill_price
        position = self._get_or_create_position(session, order)
        if order.side == OrderSide.BUY:
            # 解冻提交时预留的资金，按实际成交额和佣金结算，多冻结部分退回可用现金。
            reserved_cash = self._reserved_cash(order, execution_price=fill_price)
            total_cost = gross_amount + commission
            account.cash_frozen -= reserved_cash
            account.cash_available += reserved_cash - total_cost
            # 佣金计入买入成本，确保后续卖出时实现盈亏计算准确。
            position.avg_cost = self._weighted_avg_cost(
                current_quantity=position.quantity,
                current_avg_cost=position.avg_cost,
                fill_quantity=order.quantity,
                fill_cost=total_cost,
            )
            position.quantity += order.quantity
            position.available_quantity += order.quantity
            cash_amount = -total_cost
            ledger_event = "BUY_FILL"
        else:
            # 卖出所得扣除佣金后进入现金，并按原平均成本确认已实现盈亏。
            net_proceeds = gross_amount - commission
            realized_pnl = (fill_price - position.avg_cost) * order.quantity - commission
            account.cash_available += net_proceeds
            account.realized_pnl += realized_pnl
            position.quantity -= order.quantity
            # 清仓后不再保留历史平均成本，避免污染下一轮建仓。
            if position.quantity <= 0:
                position.quantity = Decimal("0")
                position.avg_cost = Decimal("0")
            cash_amount = net_proceeds
            ledger_event = "SELL_FILL"

        # 用本次成交价刷新持仓估值，并同步订单的最终成交信息。
        position.market_price = fill_price
        position.market_value = position.quantity * fill_price
        position.unrealized_pnl = (
            position.market_value - (position.quantity * position.avg_cost)
        )
        order.filled_quantity = order.quantity
        order.avg_fill_price = fill_price
        # 先 flush 成交和持仓变化，后续账户汇总查询才能读到最新值。
        session.flush()

        # 账户级未实现盈亏和权益由所有持仓汇总得出。
        account.unrealized_pnl = self._total_unrealized_pnl(session, account.account_id)
        account.equity = account.cash_available + account.cash_frozen + self._total_market_value(
            session, account.account_id
        )
        # 每笔成交对应一条现金流水，保留变动金额和变动后的可用余额。
        session.add(
            CashLedger(
                ledger_id=new_id("cash"),
                account_id=order.account_id,
                order_id=order.order_id,
                fill_id=fill.fill_id,
                event_type=ledger_event,
                amount=cash_amount,
                currency=account.base_currency,
                balance_after=account.cash_available,
            )
        )
        # 账本更新完成后再将订单推进到 FILLED。
        self.order_service.transition_order(
            session, trace_id, order, OrderStatus.FILLED, "LOCAL_SIM_ORDER_FILLED"
        )
        # 审计日志记录结算结果，便于按 trace_id 复盘完整交易过程。
        self.order_service.record_event(
            session,
            trace_id,
            "LOCAL_SIM_LEDGER_UPDATED",
            {"order_id": order.order_id, "fill_id": fill.fill_id},
            {
                "cash_available": str(account.cash_available),
                "position_quantity": str(position.quantity),
                "realized_pnl": str(account.realized_pnl),
                "unrealized_pnl": str(account.unrealized_pnl),
            },
        )
        # 同时保存 Broker 成交原始事件，broker_fill_id 用于未来的成交去重。
        self._record_broker_event(
            session,
            trace_id,
            order,
            event_type="FILL",
            broker_fill_id=fill.broker_fill_id,
            raw_payload={
                "fill_id": fill.fill_id,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "commission": str(fill.commission),
            },
        )
        # Outbox 事件将订单和持仓变化可靠地交给通知、对账等下游模块。
        self.order_service.enqueue_event(
            session,
            trace_id,
            event_type="ORDER_FILLED",
            aggregate_type="ORDER",
            aggregate_id=order.order_id,
            payload={"order_id": order.order_id, "fill_id": fill.fill_id},
        )
        self.order_service.enqueue_event(
            session,
            trace_id,
            event_type="POSITION_UPDATED",
            aggregate_type="POSITION",
            aggregate_id=f"{order.account_id}:{order.symbol}",
            payload={"account_id": order.account_id, "symbol": order.symbol},
        )

    def _reserved_cash(
        self,
        order: Order,
        quote: MarketQuote | None = None,
        execution_price: Decimal | None = None,
    ) -> Decimal:
        """计算订单需要冻结的最大现金，包括预计佣金。"""

        # 限价单按委托价预留，避免成交前资金被其他订单占用。
        if order.limit_price is not None:
            gross_amount = order.quantity * order.limit_price
        # 市价单提交时没有限价，只能按当前卖一价或最新价预留。
        elif quote is not None:
            price = quote.ask_price or quote.last_price
            gross_amount = order.quantity * price
        # 成交结算阶段允许用实际成交价还原市价单此前冻结的金额。
        elif execution_price is not None:
            gross_amount = order.quantity * execution_price
        else:
            raise ValueError("Market order reservation requires a quote")
        return gross_amount + self._commission(gross_amount)

    def _commission(self, gross_amount: Decimal) -> Decimal:
        """按照费率和最低佣金计算单笔成交费用。"""

        # 默认费率为 0，因此本地开发不会产生隐含费用。
        if self.config.commission_rate == 0:
            return Decimal("0")
        return max(gross_amount * self.config.commission_rate, self.config.minimum_commission)

    @staticmethod
    def _weighted_avg_cost(
        current_quantity: Decimal,
        current_avg_cost: Decimal,
        fill_quantity: Decimal,
        fill_cost: Decimal,
    ) -> Decimal:
        """将原持仓成本和本次含佣金买入成本合并为新的单位成本。"""

        total_quantity = current_quantity + fill_quantity
        if total_quantity <= 0:
            return Decimal("0")
        return ((current_quantity * current_avg_cost) + fill_cost) / total_quantity

    @staticmethod
    def _find_position(session: Session, account_id: str, symbol: str) -> Position | None:
        """查询账户在指定标的上的唯一持仓。"""

        return session.scalar(
            select(Position).where(
                Position.account_id == account_id,
                Position.symbol == symbol,
            )
        )

    def _get_or_create_position(self, session: Session, order: Order) -> Position:
        """获取订单对应持仓，首次买入时创建零值持仓记录。"""

        position = self._find_position(session, order.account_id, order.symbol)
        if position is not None:
            return position
        position = Position(
            account_id=order.account_id,
            symbol=order.symbol,
            quantity=Decimal("0"),
            available_quantity=Decimal("0"),
            avg_cost=Decimal("0"),
            market_price=Decimal("0"),
            market_value=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        )
        session.add(position)
        return position

    @staticmethod
    def _total_market_value(session: Session, account_id: str) -> Decimal:
        """汇总账户所有持仓市值，用于重算账户权益。"""

        positions = session.scalars(select(Position).where(Position.account_id == account_id)).all()
        return sum((position.market_value for position in positions), Decimal("0"))

    @staticmethod
    def _total_unrealized_pnl(session: Session, account_id: str) -> Decimal:
        """汇总账户所有持仓的未实现盈亏。"""

        positions = session.scalars(select(Position).where(Position.account_id == account_id)).all()
        return sum((position.unrealized_pnl for position in positions), Decimal("0"))

    def _record_broker_event(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        event_type: str,
        raw_payload: dict,
        broker_fill_id: str | None = None,
    ) -> None:
        """保存模拟券商返回的原始事件，作为审计和回放依据。"""

        session.add(
            BrokerEvent(
                broker_event_id=new_id("broker_evt"),
                trace_id=trace_id,
                broker_name=self.broker_name,
                broker_order_id=order.broker_order_id,
                broker_fill_id=broker_fill_id,
                event_type=event_type,
                raw_payload=raw_payload,
                processed_at=datetime.now(UTC),
            )
        )
