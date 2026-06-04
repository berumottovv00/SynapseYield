from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse_yield.domain.enums import OrderSide, OrderStatus, OrderType
from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import BrokerOrderResult, MarketQuote
from synapse_yield.harness.order_service import OrderService
from synapse_yield.storage.models import Account, CashLedger, Fill, Order, Position


class LocalSimBroker:
    """本地模拟盘 Broker，用于在无真实券商接入时模拟订单提交和成交。"""

    # Broker 名称会写入或用于区分不同交易通道。
    broker_name = "local_sim"

    def __init__(self, order_service: OrderService | None = None):
        # 复用订单服务统一推进状态机和审计日志。
        self.order_service = order_service or OrderService()

    def submit_order(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        quote: MarketQuote,
    ) -> BrokerOrderResult:
        """提交订单到本地模拟盘，并根据行情尝试立即撮合成交。"""

        # 只有风控通过的订单才能进入 Broker 提交流程。
        if order.status != OrderStatus.RISK_APPROVED:
            return BrokerOrderResult(
                accepted=False,
                message=f"Order status {order.status} is not submittable",
            )

        # 模拟 Broker 接收订单，生成本地模拟盘侧订单 ID。
        broker_order_id = new_id("ls_order")
        self.order_service.transition_order(
            session,
            trace_id,
            order,
            OrderStatus.SUBMITTING,
            "LOCAL_SIM_ORDER_SUBMITTING",
        )
        order.broker_order_id = broker_order_id
        self.order_service.transition_order(
            session,
            trace_id,
            order,
            OrderStatus.SUBMITTED,
            "LOCAL_SIM_ORDER_SUBMITTED",
        )

        # 根据订单类型、限价和当前行情判断是否可以成交。
        fill_price = self._fill_price(order, quote)
        if fill_price is not None:
            self._apply_fill(session, trace_id, order, fill_price)

        return BrokerOrderResult(
            accepted=True,
            broker_order_id=broker_order_id,
            message="Order accepted by local simulator",
        )

    def _fill_price(self, order: Order, quote: MarketQuote) -> Decimal | None:
        """计算本地模拟盘的成交价格；返回 None 表示暂不成交。"""

        # 市价单直接按最新价成交。
        if order.order_type == OrderType.MARKET:
            return quote.last_price

        # 限价单缺少限价时无法成交。
        if order.limit_price is None:
            return None

        # 买入限价需要覆盖卖一价；没有卖一价时退回使用最新价。
        if order.side == OrderSide.BUY:
            ask_price = quote.ask_price or quote.last_price
            return ask_price if order.limit_price >= ask_price else None

        # 卖出限价需要不高于买一价；没有买一价时退回使用最新价。
        bid_price = quote.bid_price or quote.last_price
        return bid_price if order.limit_price <= bid_price else None

    def _apply_fill(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        fill_price: Decimal,
    ) -> None:
        """落地成交结果，并同步更新资金、持仓、现金流水和订单状态。"""

        account = session.get(Account, order.account_id)
        if account is None:
            raise ValueError(f"Account {order.account_id} does not exist")

        # 记录成交明细，当前初版模拟盘按整笔订单一次性成交处理。
        fill = Fill(
            fill_id=new_id("fill"),
            order_id=order.order_id,
            broker_fill_id=new_id("ls_fill"),
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=Decimal("0"),
            fill_time=datetime.now(UTC),
        )
        session.add(fill)

        gross_amount = order.quantity * fill_price
        position = self._get_or_create_position(session, order)

        # 买入会扣减现金、增加持仓，并按加权平均法更新持仓成本。
        if order.side == OrderSide.BUY:
            account.cash_available -= gross_amount
            position.avg_cost = self._weighted_avg_cost(
                current_quantity=position.quantity,
                current_avg_cost=position.avg_cost,
                fill_quantity=order.quantity,
                fill_price=fill_price,
            )
            position.quantity += order.quantity
            position.available_quantity += order.quantity
            cash_amount = -gross_amount
            ledger_event = "BUY_FILL"
        else:
            # 卖出会增加现金、减少持仓；清仓后成本重置为 0。
            account.cash_available += gross_amount
            position.quantity -= order.quantity
            position.available_quantity -= order.quantity
            if position.quantity <= 0:
                position.avg_cost = Decimal("0")
            cash_amount = gross_amount
            ledger_event = "SELL_FILL"

        # 使用本次成交价刷新持仓市值，并据此重算账户权益。
        position.market_price = fill_price
        position.market_value = position.quantity * fill_price
        session.flush()
        account.equity = account.cash_available + account.cash_frozen + self._total_market_value(
            session,
            account.account_id,
        )

        # 订单在初版模拟盘中按全量成交处理。
        order.filled_quantity = order.quantity
        order.avg_fill_price = fill_price
        session.flush()

        # 写入现金流水，保留成交对资金余额的影响。
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
        # 最后推进订单到 FILLED，并由订单服务记录状态迁移审计。
        self.order_service.transition_order(
            session,
            trace_id,
            order,
            OrderStatus.FILLED,
            "LOCAL_SIM_ORDER_FILLED",
        )

    @staticmethod
    def _weighted_avg_cost(
        current_quantity: Decimal,
        current_avg_cost: Decimal,
        fill_quantity: Decimal,
        fill_price: Decimal,
    ) -> Decimal:
        """根据原持仓和本次买入成交计算新的加权平均成本。"""

        total_quantity = current_quantity + fill_quantity
        if total_quantity <= 0:
            return Decimal("0")
        return ((current_quantity * current_avg_cost) + (fill_quantity * fill_price)) / total_quantity

    @staticmethod
    def _get_or_create_position(session: Session, order: Order) -> Position:
        """获取订单对应持仓；不存在时创建一条空持仓。"""

        position = session.scalar(
            select(Position).where(
                Position.account_id == order.account_id,
                Position.symbol == order.symbol,
            )
        )
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
        """汇总账户下所有持仓市值，用于计算账户权益。"""

        positions = session.scalars(select(Position).where(Position.account_id == account_id)).all()
        return sum((position.market_value for position in positions), Decimal("0"))
