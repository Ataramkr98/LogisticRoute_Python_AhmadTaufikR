"""The demo seeder must be safely re-runnable.

``seed_demo`` is documented as idempotent, and reviewers routinely re-run it.
The risk is not that it crashes -- a crash is obvious -- but that it silently
accumulates rows: orphaned planning runs, duplicate audit entries, stale GPS
breadcrumbs or a growing pile of resolved exceptions. None of those break the
seeder, yet each one makes the dashboards and reports show data that does not
correspond to the current demo state.
"""

import pytest
from django.core.management import call_command

from apps.audit.models import AuditLog
from apps.exceptions.models import OperationException
from apps.planning.models import PlanningRun, RoutePlan
from apps.proof_of_delivery.models import ProofOfDelivery
from apps.tracking.models import DriverEvent, VehicleLocation

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _snapshot():
    return {
        "route_plans": RoutePlan.objects.count(),
        "planning_runs": PlanningRun.objects.count(),
        "driver_events": DriverEvent.objects.count(),
        "vehicle_locations": VehicleLocation.objects.count(),
        "proofs": ProofOfDelivery.objects.count(),
        "route_plan_audit": AuditLog.objects.filter(action__startswith="route_plan.").count(),
        "open_exceptions": OperationException.objects.filter(
            status=OperationException.Status.OPEN
        ).count(),
        "resolved_exceptions": OperationException.objects.filter(
            status=OperationException.Status.RESOLVED
        ).count(),
    }


def test_seed_demo_is_idempotent_across_runs():
    call_command("seed_demo", verbosity=0)
    first = _snapshot()
    call_command("seed_demo", verbosity=0)
    second = _snapshot()

    assert first == second, (
        "re-running seed_demo changed the row counts, so it accumulates data "
        f"instead of replacing it:\n  first : {first}\n  second: {second}"
    )


def test_seed_demo_leaves_no_orphaned_operational_rows():
    call_command("seed_demo", verbosity=0)
    call_command("seed_demo", verbosity=0)

    # GPS breadcrumbs are only ever written with a route. A route-less row means
    # a deleted plan's trail survived the reset and would draw a phantom path on
    # the live map.
    orphaned_pings = VehicleLocation.objects.filter(route__isnull=True).count()
    assert orphaned_pings == 0, f"{orphaned_pings} orphaned vehicle location(s) remain"

    # A run with no plan means the previous seed's run was stranded when its
    # plan was replaced, and it still shows up in the planning history.
    stranded_runs = [run.pk for run in PlanningRun.objects.all() if not run.route_plans.exists()]
    assert not stranded_runs, f"planning run(s) {stranded_runs} have no route plan"

    # Audit entries whose subject no longer exists read as corrupt data.
    live_plan_ids = {str(pk) for pk in RoutePlan.objects.values_list("pk", flat=True)}
    dangling_audit = [
        log.pk
        for log in AuditLog.objects.filter(action__startswith="route_plan.")
        if log.resource_id not in live_plan_ids
    ]
    assert not dangling_audit, f"audit log(s) {dangling_audit} reference deleted plans"


def test_seed_demo_produces_a_usable_operational_picture():
    """Guard against the seed quietly degrading to an empty dashboard."""
    call_command("seed_demo", verbosity=0)

    assert RoutePlan.objects.count() >= 2, "the demo needs at least a two-day plan history"
    assert DriverEvent.objects.exists(), "the driver app and timeline would be empty"
    assert VehicleLocation.objects.exists(), "the live map would be empty"
    assert ProofOfDelivery.objects.exists(), "the proof-of-delivery view would be empty"
    assert OperationException.objects.exists(), "the exceptions page would be empty"
