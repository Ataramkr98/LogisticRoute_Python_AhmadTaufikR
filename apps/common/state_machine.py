"""Legal state transitions for the operational lifecycle.

This module is the single authority for which status may follow which. Every
other place in the codebase either calls :func:`assert_transition` or is a
report; the alternative was hand-rolled ``if status != X: raise`` checks
scattered across the service layer, which drift apart and cannot be reviewed in
one place.

Two properties make the tables trustworthy:

* every key and every value must be a value the corresponding model's
  ``Status`` TextChoices actually defines, and
* any status with no outgoing edges is terminal and nothing may leave it.

Both are asserted at import time by :func:`_validate_table`, so adding a status
to a model without deciding where it belongs fails immediately and loudly rather
than producing an entity that can never move.
"""

from django.core.exceptions import ValidationError


class InvalidTransition(ValidationError):
    """A requested status change is not legal from the current state.

    Subclasses ``ValidationError`` so DRF renders it as a 400 with the
    explanatory message intact, while callers inside a service can still catch
    it as ``ValueError`` via :class:`DriverActionConflict`.
    """


def assert_transition(current, target, transitions, *, entity="entity", error=InvalidTransition):
    """Raise unless ``current -> target`` is listed in ``transitions``.

    ``error`` lets a caller raise its own domain exception while still deferring
    the *decision* to this table, so the tracking service reports a
    ``DriverActionConflict`` without re-implementing the rules.
    """
    if current == target:
        # Re-applying the current state is a no-op, not an illegal move. Several
        # call sites re-assert a status they may already hold after a retry.
        return
    allowed = transitions.get(current, set())
    if target not in allowed:
        legal = ", ".join(sorted(allowed)) or "no further transitions"
        raise error(f"{entity} cannot move from {current} to {target}. Legal next: {legal}.")


ORDER_TRANSITIONS = {
    "DRAFT": {"READY", "CANCELLED"},
    "READY": {"PLANNED", "UNASSIGNED", "CANCELLED"},
    "UNASSIGNED": {"READY", "PLANNED", "CANCELLED"},
    "PLANNED": {"DISPATCHED", "READY", "UNASSIGNED", "CANCELLED"},
    "DISPATCHED": {"IN_PROGRESS", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "FAILED", "PARTIALLY_COMPLETED", "CANCELLED"},
    "FAILED": {"READY", "CANCELLED"},
    "PARTIALLY_COMPLETED": {"COMPLETED", "FAILED"},
    "COMPLETED": set(),
    # Cancellation is terminal for an order; the seeder and admin both reach it,
    # and no workflow returns an order to service afterwards.
    "CANCELLED": set(),
}

ROUTE_TRANSITIONS = {
    "DRAFT": {"DISPATCHED", "CANCELLED"},
    "DISPATCHED": {"IN_PROGRESS", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}

STOP_TRANSITIONS = {
    "PENDING": {"EN_ROUTE", "ARRIVED", "SKIPPED", "CANCELLED"},
    "EN_ROUTE": {"ARRIVED", "FAILED", "SKIPPED", "CANCELLED"},
    "ARRIVED": {"SERVICING", "COMPLETED", "FAILED", "SKIPPED"},
    "SERVICING": {"COMPLETED", "FAILED", "SKIPPED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "SKIPPED": set(),
    "CANCELLED": set(),
}

PLAN_TRANSITIONS = {
    "DRAFT": {"OPTIMIZED", "REVIEW", "SUPERSEDED", "CANCELLED"},
    "OPTIMIZED": {"REVIEW", "DISPATCHED", "SUPERSEDED", "CANCELLED"},
    "REVIEW": {"DISPATCHED", "OPTIMIZED", "SUPERSEDED", "CANCELLED"},
    "DISPATCHED": {"IN_PROGRESS", "COMPLETED", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "CANCELLED"},
    "SUPERSEDED": set(),
    "COMPLETED": set(),
    "CANCELLED": set(),
}

RUN_TRANSITIONS = {
    "DRAFT": {"MATRIX_PENDING", "CANCELLED"},
    "MATRIX_PENDING": {"OPTIMIZING", "FAILED", "CANCELLED"},
    "OPTIMIZING": {"OPTIMIZED", "FAILED", "CANCELLED"},
    "OPTIMIZED": {"REVIEW", "DISPATCHED", "SUPERSEDED", "CANCELLED"},
    "REVIEW": {"DISPATCHED", "SUPERSEDED", "CANCELLED"},
    "DISPATCHED": {"IN_PROGRESS", "COMPLETED", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "CANCELLED"},
    "FAILED": set(),
    "SUPERSEDED": set(),
    "COMPLETED": set(),
    "CANCELLED": set(),
}

#: label used in operator-facing messages
ENTITY_LABELS = {
    "order": "Order",
    "route": "Route",
    "stop": "Stop",
    "plan": "Route plan",
    "run": "Planning run",
}


def _choices(name):
    """Return the status values of a model, imported lazily.

    Imported inside the function because the model layer imports services that
    import this module; resolving at import time would create a cycle.
    """
    if name == "order":
        from apps.orders.models import Order

        return set(Order.Status.values)
    if name == "route":
        from apps.planning.models import Route

        return set(Route.Status.values)
    if name == "stop":
        from apps.planning.models import RouteStop

        return set(RouteStop.Status.values)
    if name == "plan":
        from apps.planning.models import RoutePlan

        return set(RoutePlan.Status.values)
    if name == "run":
        from apps.planning.models import PlanningRun

        return set(PlanningRun.Status.values)
    raise KeyError(name)


def _validate_table(entity, table):
    """Fail loudly at import if the table disagrees with the model's choices."""
    declared = _choices(entity)
    mentioned = set(table) | {target for targets in table.values() for target in targets}
    unknown = mentioned - declared
    if unknown:
        raise RuntimeError(
            f"{entity} transition table references status values that "
            f"{entity} does not define: {sorted(unknown)}. "
            "Update the model or the table."
        )
    missing = declared - set(table)
    if missing:
        raise RuntimeError(
            f"{entity} transition table has no entry for: {sorted(missing)}. "
            "Decide where each status may go, or mark it terminal."
        )


def validate_transition_tables():
    """Validate every table. Exposed so the test suite can call it explicitly."""
    for entity, table in (
        ("order", ORDER_TRANSITIONS),
        ("route", ROUTE_TRANSITIONS),
        ("stop", STOP_TRANSITIONS),
        ("plan", PLAN_TRANSITIONS),
        ("run", RUN_TRANSITIONS),
    ):
        _validate_table(entity, table)
    return True


validate_transition_tables()
