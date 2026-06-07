from decimal import Decimal
from hashlib import sha256

from sqlalchemy.orm import Session

from synapse_yield.domain.enums import OrderStatus, OutboxStatus, RiskDecisionStatus
from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import MarketQuote, OrderIntent
from synapse_yield.domain.state_machine import assert_order_transition
from synapse_yield.risk.engine import RiskEngine
from synapse_yield.storage.models import AuditLog, Order, OutboxEvent, RiskDecision


class OrderService:
    """订单编排服务，负责连接下单意图、风控、状态机、审计和 Outbox。"""

    def __init__(self, risk_engine: RiskEngine | None = None):
        # 默认使用基础风控引擎；测试或扩展场景可以注入自定义实现。
        self.risk_engine = risk_engine or RiskEngine()

    def create_order_from_intent(self, session: Session, trace_id: str, intent: OrderIntent) -> Order:
        """根据下单意图创建本地订单记录，并写入创建审计日志。"""

        # order_id 是系统内部主键，client_order_id 用于提交给 Broker 或模拟盘。
        order_id = new_id("ord")
        client_order_id = new_id("client")
        # 幂等键由关键业务字段哈希生成，用于识别重复下单请求。
        idempotency_key = self._idempotency_key(intent)

        order = Order(
            order_id=order_id,
            client_order_id=client_order_id,
            account_id=intent.account_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            status=OrderStatus.CREATED,
            quantity=intent.quantity,
            filled_quantity=Decimal("0"),
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force,
            source_signal_id=intent.source_signal_id,
            idempotency_key=idempotency_key,
            raw_request=intent.model_dump(mode="json"),
        )
        session.add(order)
        # 创建订单时立即记录审计事件，保留下单输入和生成的订单标识。
        self._audit(
            session=session,
            trace_id=trace_id,
            event_type="ORDER_CREATED",
            input_snapshot=intent.model_dump(mode="json"),
            output_snapshot={"order_id": order_id, "client_order_id": client_order_id},
            previous_state=None,
            next_state=OrderStatus.CREATED,
        )
        return order

    def run_risk_check(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        quote: MarketQuote | None = None,
        market_is_open: bool | None = None,
    ) -> RiskDecision:
        """对已创建订单执行风控，并根据结果推进订单状态。

        quote 用于市价单估值和限价偏离检查；market_is_open 由交易日历模块提供。
        """

        # 从订单记录还原下单意图，确保风控使用的是已落库的订单数据。
        intent = OrderIntent(
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.limit_price,
            time_in_force=order.time_in_force,
            source_signal_id=order.source_signal_id,
        )
        # 排除当前订单，避免它在重复订单和每日订单统计中把自己计算进去。
        result = self.risk_engine.check(
            session,
            intent,
            exclude_order_id=order.order_id,
            quote=quote,
            market_is_open=market_is_open,
        )
        # 将风控引擎的布尔结果转换为领域枚举，便于持久化和查询。
        decision_status = (
            RiskDecisionStatus.APPROVED if result.approved else RiskDecisionStatus.REJECTED
        )
        risk_decision = RiskDecision(
            risk_decision_id=new_id("risk"),
            trace_id=trace_id,
            signal_id=order.source_signal_id,
            decision=decision_status,
            reason_code=result.reason_code,
            message=result.message,
            checked_rules=result.checked_rules,
            input_snapshot=result.input_snapshot,
        )
        session.add(risk_decision)
        session.flush()
        order.risk_decision_id = risk_decision.risk_decision_id

        # 风控通过进入 RISK_APPROVED，未通过进入 RISK_REJECTED。
        target_status = OrderStatus.RISK_APPROVED if result.approved else OrderStatus.RISK_REJECTED
        self.transition_order(session, trace_id, order, target_status, "RISK_CHECK_COMPLETED")

        # 只有通过风控的订单才写入 Outbox，等待后续提交模块消费。
        if result.approved:
            self._enqueue_outbox(
                session=session,
                trace_id=trace_id,
                event_type="ORDER_READY_TO_SUBMIT",
                aggregate_id=order.order_id,
                payload={"order_id": order.order_id, "client_order_id": order.client_order_id},
            )

        return risk_decision

    def transition_order(
        self,
        session: Session,
        trace_id: str,
        order: Order,
        target_status: OrderStatus,
        event_type: str,
    ) -> None:
        """统一执行订单状态迁移，并为每次迁移写入审计日志。"""

        previous_status = order.status
        # 状态迁移必须先经过状态机校验，避免非法生命周期变更。
        assert_order_transition(previous_status, target_status)
        order.status = target_status
        self._audit(
            session=session,
            trace_id=trace_id,
            event_type=event_type,
            input_snapshot={"order_id": order.order_id},
            output_snapshot={"status": target_status.value},
            previous_state=previous_status,
            next_state=target_status,
        )

    @classmethod
    def record_event(
        cls,
        session: Session,
        trace_id: str,
        event_type: str,
        input_snapshot: dict,
        output_snapshot: dict,
    ) -> None:
        """记录不涉及订单状态迁移的 Harness 审计事件。"""

        # 资源冻结、释放和账本更新没有订单前后状态，但仍必须保留审计快照。
        cls._audit(
            session=session,
            trace_id=trace_id,
            event_type=event_type,
            input_snapshot=input_snapshot,
            output_snapshot=output_snapshot,
            previous_state=None,
            next_state=None,
        )

    @staticmethod
    def _idempotency_key(intent: OrderIntent) -> str:
        """基于订单关键字段生成幂等键，辅助识别重复请求。"""

        raw = "|".join(
            [
                intent.account_id,
                intent.strategy_name or "",
                intent.source_signal_id or "",
                intent.symbol,
                intent.side.value,
                str(intent.quantity),
                str(intent.limit_price or ""),
            ]
        )
        return sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _audit(
        session: Session,
        trace_id: str,
        event_type: str,
        input_snapshot: dict,
        output_snapshot: dict,
        previous_state: OrderStatus | None,
        next_state: OrderStatus | None,
    ) -> None:
        """追加审计日志，记录一次订单流程动作的输入、输出和状态变化。"""

        session.add(
            AuditLog(
                audit_id=new_id("audit"),
                trace_id=trace_id,
                actor_type="HARNESS",
                actor_name="OrderService",
                event_type=event_type,
                input_snapshot=input_snapshot,
                output_snapshot=output_snapshot,
                previous_state=previous_state.value if previous_state else None,
                next_state=next_state.value if next_state else None,
            )
        )

    @staticmethod
    def _enqueue_outbox(
        session: Session,
        trace_id: str,
        event_type: str,
        aggregate_id: str,
        payload: dict,
        aggregate_type: str = "ORDER",
    ) -> None:
        """写入待发布的领域事件，供异步提交或消息发布流程消费。"""

        # 事件与当前数据库事务一起提交，避免业务数据成功但通知事件丢失。
        session.add(
            OutboxEvent(
                event_id=new_id("evt"),
                trace_id=trace_id,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                status=OutboxStatus.PENDING,
            )
        )

    @classmethod
    def enqueue_event(
        cls,
        session: Session,
        trace_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
    ) -> None:
        """向 Outbox 追加领域事件。"""

        # 对外暴露统一入口，Broker 无需依赖私有的 _enqueue_outbox 方法。
        cls._enqueue_outbox(
            session=session,
            trace_id=trace_id,
            event_type=event_type,
            aggregate_id=aggregate_id,
            payload=payload,
            aggregate_type=aggregate_type,
        )
