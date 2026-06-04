from synapse_yield.domain.enums import OrderStatus


# 订单状态机的邻接表：key 是当前状态，value 是允许进入的下一批状态。
# 这里集中定义合法迁移，业务代码只需要调用 assert_order_transition 做校验。
ALLOWED_ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    # 新建订单先进入风控；风控拒绝或系统异常会直接结束或失败。
    OrderStatus.CREATED: {
        OrderStatus.RISK_APPROVED,
        OrderStatus.RISK_REJECTED,
        OrderStatus.FAILED,
    },
    # 风控拒绝是终态，订单不能再提交给 Broker。
    OrderStatus.RISK_REJECTED: set(),
    # 风控通过后才能提交；提交前发生异常则进入 FAILED。
    OrderStatus.RISK_APPROVED: {
        OrderStatus.SUBMITTING,
        OrderStatus.FAILED,
    },
    # SUBMITTING 表示请求已发出但 Broker 尚未确认接收。
    OrderStatus.SUBMITTING: {
        OrderStatus.SUBMITTED,
        OrderStatus.FAILED,
        OrderStatus.RECONCILING,
    },
    # Broker 接收后，订单可以成交、撤单、失败，或因状态不确定进入对账。
    OrderStatus.SUBMITTED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
        OrderStatus.RECONCILING,
    },
    # 部分成交后，剩余数量可能继续成交，也可能被撤销或等待对账修复。
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.RECONCILING,
    },
    # 全部成交是正常终态。
    OrderStatus.FILLED: set(),
    # 撤单请求发出后，Broker 仍可能先回报部分或全部成交。
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.CANCELLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.RECONCILING,
    },
    # 已撤销是终态，未成交部分不再有效。
    OrderStatus.CANCELLED: set(),
    # FAILED 通常是终态；保留进入 RECONCILING 的通道用于修复误判或延迟回报。
    OrderStatus.FAILED: {
        OrderStatus.RECONCILING,
    },
    # 对账态用于从 Broker 或本地账本重新确认真实状态，再回到确定状态。
    OrderStatus.RECONCILING: {
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    },
}


class InvalidOrderTransition(ValueError):
    """订单状态迁移不在 ALLOWED_ORDER_TRANSITIONS 中时抛出。"""

    pass


def assert_order_transition(current: OrderStatus, target: OrderStatus) -> None:
    """校验订单状态是否允许从 current 迁移到 target。"""

    if target not in ALLOWED_ORDER_TRANSITIONS[current]:
        raise InvalidOrderTransition(f"Cannot transition order from {current} to {target}")
