import pytest

from synapse_yield.domain.enums import OrderStatus
from synapse_yield.domain.state_machine import InvalidOrderTransition, assert_order_transition


def test_allows_risk_approved_order_to_submit() -> None:
    assert_order_transition(OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING)


def test_rejects_filled_order_to_submit() -> None:
    with pytest.raises(InvalidOrderTransition):
        assert_order_transition(OrderStatus.FILLED, OrderStatus.SUBMITTING)

