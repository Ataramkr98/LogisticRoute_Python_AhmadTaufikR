"""Operational history for the demo organization.

The dashboard, planning review, route detail, live map, driver app and reports
all read from the operational half of the schema (planning runs, route plans,
routes, stops, driver events, GPS pings, proofs of delivery). Without any of it
those pages can only ever render empty states, which makes the application look
unfinished even though the features work.

This module drives the real service layer -- ``optimize_run`` and
``dispatch_plan`` for planning, ``record_driver_action`` and ``record_location``
for field execution -- rather than inserting rows directly. That way the demo
data respects every state machine, side effect, audit record and notification the
production code path would produce, and the seed cannot drift out of sync with
the services it is meant to exercise.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.dispatch.services import dispatch_plan
from apps.exceptions.models import OperationException
from apps.orders.models import Order
from apps.planning.models import PlanningRun, PlanningRunOrder, Route, RoutePlan
from apps.proof_of_delivery.models import ProofOfDelivery
from apps.tracking.models import VehicleLocation
from apps.tracking.services import record_driver_action, record_location

# The demo tells a story that a reviewer can follow: yesterday closed out
# completely, today is mid-flight. Only "today" is left running so the live map
# and driver app have something to show.
ACTIVE_ROUTE_STOPS_COMPLETED = 2


def _midnight(day, zone):
    return timezone.make_aware(datetime.combine(day, time.min), zone)


def _at(day, zone, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), zone)


def _next_day(day, zone):
    return _midnight(day + timedelta(days=1), zone) - timedelta(seconds=1)


@transaction.atomic
def build_operational_history(*, organization, depot, users, drivers, vehicles, today, zone):
    """Create planning runs, routes and field-execution history.

    Returns a summary dict so the calling command can report what it produced.
    """
    summary = {}
    summary["yesterday"] = _seed_completed_day(
        organization=organization,
        depot=depot,
        users=users,
        drivers=drivers,
        vehicles=vehicles,
        day=today - timedelta(days=1),
        zone=zone,
    )
    summary["today"] = _seed_active_day(
        organization=organization,
        depot=depot,
        users=users,
        drivers=drivers,
        vehicles=vehicles,
        day=today,
        zone=zone,
    )
    return summary


def _reset_day(day, *, organization):
    """Clear any previous demo history for a service date so the seed is idempotent.

    Route plans for a date are fully owned by this command, and clearing them
    cascades to routes, stops, events and proofs of delivery. Orders are pushed
    back to READY so the planner has work to do on every run.

    The planning runs, the audit entries and the operation exceptions the
    previous run produced are removed as well. Deleting only the plans would
    strand their runs (leaving orphaned rows with no plans in the planning
    history) and would leave audit entries pointing at plans that no longer
    exist, which reads as corrupt data. Exceptions raised by the recorded field
    failures would likewise pile up across runs, so they are cleared by the
    service date they belong to.
    """
    plans = RoutePlan.objects.filter(service_date=day)
    plan_ids = [str(pk) for pk in plans.values_list("pk", flat=True)]
    Order.objects.filter(
        route_stops__route__route_plan__in=plans,
    ).update(status=Order.Status.READY, assigned_route_stop=None)
    plans.delete()
    PlanningRun.objects.filter(service_date=day).delete()
    # Audit entries outlive the rows they describe, so remove the ones this
    # command produced; otherwise the audit view lists dispatches of plans that
    # no longer exist.
    AuditLog.objects.filter(action__startswith="route_plan.", resource_id__in=plan_ids).delete()
    # ``VehicleLocation.route`` is SET_NULL, so deleting a plan's routes orphans
    # its GPS breadcrumbs instead of cascading them away. Left behind they
    # accumulate on every re-seed and the live map renders phantom trails for
    # vehicles that are not running. They are only ever written with a route, so
    # a route-less row belonging to this organization is stale by definition.
    VehicleLocation.objects.filter(organization=organization, route__isnull=True).delete()
    # Scope the cleanup the same way the planning rows are scoped, by the
    # service date the demo owns, rather than by the exception type: relying on
    # a type filter here silently stopped matching once the recorded failure
    # changed, and repeated seeds then accumulated duplicate exceptions.
    OperationException.objects.filter(
        organization=organization, created_at__date=day
    ).delete()


def _create_run(*, organization, depot, profile, users, drivers, vehicles, day, zone):
    run = PlanningRun.objects.create(
        organization=organization,
        depot=depot,
        service_date=day,
        profile=profile,
        requested_by=users["demo@example.com"],
        status=PlanningRun.Status.DRAFT,
    )
    # Orders must be attached to the run: the node snapshot is built from
    # ``run.run_orders`` filtered to eligible entries, so a run with no
    # PlanningRunOrder rows produces a depot-only matrix and the solver returns
    # an empty, infeasible plan.
    for order in Order.objects.filter(organization=organization, service_date=day):
        PlanningRunOrder.objects.create(planning_run=run, order=order)
    # Pair by skill so the cold-chain driver actually crews the cold-chain
    # vehicle; a positional pairing would leave that capability unusable.
    cold_vehicles = [vehicle for vehicle in vehicles if "COLD_CHAIN" in (vehicle.skills_json or [])]
    cold_drivers = [driver for driver in drivers if "COLD_CHAIN" in (driver.skills_json or [])]
    remaining_vehicles = [vehicle for vehicle in vehicles if vehicle not in cold_vehicles]
    remaining_drivers = [driver for driver in drivers if driver not in cold_drivers]
    pairs = list(zip(cold_drivers, cold_vehicles, strict=False)) + list(
        zip(remaining_drivers, remaining_vehicles, strict=False)
    )
    for driver, vehicle in pairs:
        run.run_vehicles.create(
            vehicle=vehicle,
            driver=driver,
            availability_snapshot_json={"vehicle_version": vehicle.version},
        )
    return run


def _dispatch_if_routable(plan, *, actor, label):
    """Dispatch a plan, tolerating a plan the solver could not route.

    The solver legitimately returns no routes when nothing is feasibly
    assignable. That is a valid outcome rather than a bug, so the seed reports it
    and leaves the plan in review instead of aborting the whole command.
    """
    if not plan.routes.exists():
        return False
    if plan.routes.filter(driver__isnull=True).exists():
        return False
    dispatch_plan(plan.pk, actor=actor)
    return True


def _backdate_dispatch(plan, day, zone):
    """Move the planning and dispatch timestamps onto the evening before.

    ``optimize_run`` and ``dispatch_plan`` stamp the real wall-clock time, but
    this is historical demo data whose field events are backdated onto the
    service date. Left alone, the plan would appear to have been planned and
    dispatched after the drivers had already finished their rounds, which reads
    as corrupt data in the run history, the plan timeline and the audit view.

    The whole run is placed the evening before the service date in a plausible
    order: the solve starts, finishes, and the plan is then dispatched.
    """
    planning_run = plan.planning_run
    solved_at = _at(day - timedelta(days=1), zone, 17, 30)
    dispatched_at = _at(day - timedelta(days=1), zone, 18, 0)
    PlanningRun.objects.filter(pk=planning_run.pk).update(
        started_at=solved_at, completed_at=solved_at
    )
    planning_run.started_at = solved_at
    planning_run.completed_at = solved_at
    RoutePlan.objects.filter(pk=plan.pk).update(dispatched_at=dispatched_at)
    plan.dispatched_at = dispatched_at
    AuditLog.objects.filter(
        organization_id=plan.organization_id,
        action="route_plan.dispatched",
        resource_id=str(plan.pk),
    ).update(created_at=dispatched_at)


def _seed_completed_day(*, organization, depot, users, drivers, vehicles, day, zone):
    """A fully closed-out previous day, so history and reports are not empty."""
    from apps.optimization.services import optimize_run

    _reset_day(day, organization=organization)
    orders = list(
        Order.objects.filter(organization=organization, service_date=day).order_by("pk")
    )
    if not orders:
        return None
    run = _create_run(
        organization=organization,
        depot=depot,
        profile=organization.planning_profiles.get(active=True),
        users=users,
        drivers=drivers,
        vehicles=vehicles,
        day=day,
        zone=zone,
    )
    plan = optimize_run(run.pk)
    if plan is None:
        return None
    if not _dispatch_if_routable(plan, actor=users["demo@example.com"], label=str(day)):
        return plan
    plan.refresh_from_db()
    _backdate_dispatch(plan, day, zone)

    routes = list(plan.routes.select_related("driver").order_by("sequence"))
    for index, route in enumerate(routes):
        driver = route.driver
        if driver is None:
            continue
        # Follow the planned departure rather than a fixed hour, so the recorded
        # actuals line up with the schedule the solver produced. The final route
        # has one failed stop so the day is not suspiciously perfect, and so an
        # exception exists that the operations team has already closed out.
        _drive_route_to_completion(
            route=route,
            driver=driver,
            day=day,
            zone=zone,
            start_hour=route.start_at_planned.astimezone(zone).hour,
            fail_last_stop=index == len(routes) - 1,
            capture_proof=True,
        )
    return plan


def _seed_active_day(*, organization, depot, users, drivers, vehicles, day, zone):
    """Today's plan, dispatched and already partly executed."""
    from apps.optimization.services import optimize_run

    _reset_day(day, organization=organization)
    # Scope strictly to this service date: yesterday's already-completed orders
    # must not be re-planned into today's run.
    pending = list(
        Order.objects.filter(organization=organization, service_date=day).order_by("pk")
    )
    if not pending:
        return None
    run = _create_run(
        organization=organization,
        depot=depot,
        profile=organization.planning_profiles.get(active=True),
        users=users,
        drivers=drivers,
        vehicles=vehicles,
        day=day,
        zone=zone,
    )
    plan = optimize_run(run.pk)
    if plan is None:
        return None
    if not _dispatch_if_routable(plan, actor=users["demo@example.com"], label=str(day)):
        return plan
    plan.refresh_from_db()
    _backdate_dispatch(plan, day, zone)

    routes = list(plan.routes.select_related("driver").order_by("sequence"))
    for index, route in enumerate(routes):
        driver = route.driver
        if driver is None:
            continue
        # One route is well under way, the others have only just left the depot,
        # so the dashboard and live map show genuinely different progress states.
        # Every route still departs at its planned time: staggering the *recorded
        # progress* is what makes the day look alive, not faking the schedule.
        completed = max(ACTIVE_ROUTE_STOPS_COMPLETED - index, 1)
        _drive_route_to_completion(
            route=route,
            driver=driver,
            day=day,
            zone=zone,
            start_hour=route.start_at_planned.astimezone(zone).hour,
            fail_last_stop=index == len(routes) - 1,
            capture_proof=index == 0,
            stop_after=completed,
        )
    return plan


def _drive_route_to_completion(
    *,
    route,
    driver,
    day,
    zone,
    start_hour,
    fail_last_stop,
    capture_proof,
    stop_after=None,
):
    """Walk a route through the real driver state machine.

    Emits the same event sequence a driver app produces: ``ROUTE_STARTED``, then
    ``STOP_ARRIVED`` / ``STOP_COMPLETED`` per stop, with GPS pings interleaved.
    ``stop_after`` leaves the route deliberately unfinished for the live view.
    """
    route.refresh_from_db()
    if route.status != Route.Status.DISPATCHED:
        return

    started_at = _at(day, zone, start_hour)
    key_prefix = f"seed-{route.pk}"

    record_driver_action(
        driver=driver,
        event_type="ROUTE_STARTED",
        idempotency_key=f"{key_prefix}-start",
        route_id=route.pk,
        payload={
            "expected_version": route.version,
            "longitude": float(route.vehicle.start_location.x) if route.vehicle.start_location else None,
            "latitude": float(route.vehicle.start_location.y) if route.vehicle.start_location else None,
            "source": "driver_web",
        },
        occurred_at=started_at,
    )
    _ping(driver=driver, route=route, point=route.vehicle.start_location, at=started_at, speed_kph=0)

    order_stops = list(
        route.stops.filter(order__isnull=False).order_by("sequence")
    )
    if stop_after is not None:
        order_stops = order_stops[:stop_after]

    cursor = started_at
    for position, stop in enumerate(order_stops):
        is_last = position == len(order_stops) - 1
        should_fail = fail_last_stop and is_last

        cursor += timedelta(minutes=25)
        stop.refresh_from_db()
        record_driver_action(
            driver=driver,
            event_type="STOP_ARRIVED",
            idempotency_key=f"{key_prefix}-arrive-{stop.pk}",
            route_id=route.pk,
            stop_id=stop.pk,
            payload={
                "expected_version": stop.version,
                "longitude": float(stop.location.x),
                "latitude": float(stop.location.y),
            },
            occurred_at=cursor,
        )
        _ping(driver=driver, route=route, point=stop.location, at=cursor, speed_kph=0)

        cursor += timedelta(minutes=10)
        stop.refresh_from_db()
        if should_fail:
            record_driver_action(
                driver=driver,
                event_type="STOP_FAILED",
                idempotency_key=f"{key_prefix}-fail-{stop.pk}",
                route_id=route.pk,
                stop_id=stop.pk,
                payload={
                    "expected_version": stop.version,
                    "reason_code": "CUSTOMER_UNAVAILABLE",
                    "note": "Recipient was not available at the delivery address; a retry is required.",
                    "longitude": float(stop.location.x),
                    "latitude": float(stop.location.y),
                },
                occurred_at=cursor,
            )
            # The exception is written with ``auto_now_add``, so it would carry
            # the real wall-clock date rather than the service date being
            # replayed. Backdate it onto the route so it sorts with the rest of
            # that day's history and lands in the correct triage window.
            OperationException.objects.filter(
                organization=driver.organization, route_stop=stop
            ).update(created_at=cursor)
            continue

        record_driver_action(
            driver=driver,
            event_type="STOP_COMPLETED",
            idempotency_key=f"{key_prefix}-complete-{stop.pk}",
            route_id=route.pk,
            stop_id=stop.pk,
            payload={
                "expected_version": stop.version,
                "longitude": float(stop.location.x),
                "latitude": float(stop.location.y),
            },
            occurred_at=cursor,
        )
        stop.refresh_from_db()
        if capture_proof:
            _capture_proof(stop=stop, driver=driver, at=cursor)


def _capture_proof(*, stop, driver, at):
    if stop.order_id is None:
        return
    ProofOfDelivery.objects.update_or_create(
        route_stop=stop,
        defaults={
            "recipient_name": stop.order.customer.name,
            "recipient_relation": "Receiving staff",
            "note": "Signed for at the front desk.",
            "captured_at": at,
            "captured_location": stop.location,
        },
    )


def _ping(*, driver, route, point, at, speed_kph):
    """Record one GPS sample on the route's breadcrumb trail."""
    if point is None:
        return
    record_location(
        driver=driver,
        route=route,
        payload={
            "longitude": float(point.x),
            "latitude": float(point.y),
            "recorded_at": at,
            "speed_kph": speed_kph,
            "accuracy_m": Decimal("8.00"),
            "source": "driver_web",
        },
    )
