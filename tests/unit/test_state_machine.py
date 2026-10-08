import pytest

from apps.common.state_machine import (
    ORDER_TRANSITIONS,
    ROUTE_TRANSITIONS,
    STOP_TRANSITIONS,
    InvalidTransition,
    assert_transition,
)


def test_order_happy_path_is_legal():
    path = ["DRAFT", "READY", "PLANNED", "DISPATCHED", "IN_PROGRESS", "COMPLETED"]
    for current, target in zip(path[:-1], path[1:], strict=True):
        assert_transition(current, target, ORDER_TRANSITIONS, entity="order")


def test_dispatched_route_cannot_return_to_draft():
    with pytest.raises(InvalidTransition):
        assert_transition("DISPATCHED", "DRAFT", ROUTE_TRANSITIONS, entity="route")


def test_completed_stop_is_terminal():
    with pytest.raises(InvalidTransition):
        assert_transition("COMPLETED", "ARRIVED", STOP_TRANSITIONS, entity="stop")
