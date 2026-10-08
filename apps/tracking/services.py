from django.contrib.gis.geos import Point
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.services import record_audit
from apps.common.state_machine import (
    PLAN_TRANSITIONS,
    ROUTE_TRANSITIONS,
    RUN_TRANSITIONS,
    STOP_TRANSITIONS,
    assert_transition,
)
from apps.drivers.models import Driver
from apps.exceptions.models import OperationException
from apps.fleet.models import Vehicle
from apps.integrations.services import emit_webhook_event
from apps.orders.models import Order
from apps.planning.models import Route, RoutePlan, RouteStop

from .models import DriverEvent, VehicleLocation


class DriverActionConflict(ValueError):
    pass


ROUTE_EVENT_TYPES = {"ROUTE_STARTED"}
STOP_EVENT_TYPES = {"STOP_ARRIVED", "STOP_COMPLETED", "STOP_FAILED", "STOP_SKIPPED"}
SUPPORTED_EVENT_TYPES = ROUTE_EVENT_TYPES | STOP_EVENT_TYPES


def _check_version(instance, expected_version):
    try:
        expected_version = int(expected_version) if expected_version is not None else None
    except (TypeError, ValueError) as exc:
        raise DriverActionConflict("expected_version must be an integer") from exc
    if expected_version is not None and instance.version != expected_version:
        raise DriverActionConflict(
            f"Version conflict: expected {expected_version}, current version is {instance.version}"
        )


def _location(payload):
    longitude = payload.get("longitude")
    latitude = payload.get("latitude")
    if longitude is None or latitude is None:
        return None
    try:
        longitude = float(longitude)
        latitude = float(latitude)
    except (TypeError, ValueError) as exc:
        raise DriverActionConflict("longitude and latitude must be numeric") from exc
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise DriverActionConflict("longitude or latitude is outside WGS84 bounds")
    return Point(longitude, latitude, srid=4326)


@transaction.atomic
def record_driver_action(*, driver, event_type, idempotency_key, route_id, stop_id=None, payload=None, occurred_at=None):
    payload = payload or {}
    if not isinstance(payload, dict):
        raise DriverActionConflict("payload must be an object")
    route_completed = False
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise DriverActionConflict(f"Unsupported driver event type: {event_type}")
    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key or len(idempotency_key) > 100:
        raise DriverActionConflict("idempotency_key must contain 1 to 100 characters")
    if event_type in STOP_EVENT_TYPES and not stop_id:
        raise DriverActionConflict(f"{event_type} requires stop_id")
    if event_type in ROUTE_EVENT_TYPES and stop_id:
        raise DriverActionConflict(f"{event_type} cannot include stop_id")
    if payload.get("expected_version") is None:
        raise DriverActionConflict("expected_version is required")
    if isinstance(occurred_at, str):
        occurred_at = parse_datetime(occurred_at)
        if occurred_at is None:
            raise DriverActionConflict("occurred_at must be an ISO-8601 timestamp")
    existing = DriverEvent.objects.filter(driver=driver, idempotency_key=idempotency_key).first()
    if existing:
        if (
            existing.event_type != event_type
            or existing.route_id != int(route_id)
            or existing.route_stop_id != (int(stop_id) if stop_id else None)
        ):
            raise DriverActionConflict("Idempotency key was already used for a different event")
        return existing, True
    try:
        route = Route.objects.select_for_update().select_related("route_plan__planning_run").get(
            pk=route_id,
            driver=driver,
        )
    except (Route.DoesNotExist, TypeError, ValueError) as exc:
        raise DriverActionConflict("Route was not found for this driver") from exc
    stop = None
    if stop_id:
        try:
            stop = RouteStop.objects.select_for_update().get(pk=stop_id, route=route)
        except (RouteStop.DoesNotExist, TypeError, ValueError) as exc:
            raise DriverActionConflict("Stop was not found on this route") from exc
        _check_version(stop, payload.get("expected_version"))
    else:
        _check_version(route, payload.get("expected_version"))
    event = DriverEvent.objects.create(
        organization=driver.organization,
        route=route,
        route_stop=stop,
        driver=driver,
        event_type=event_type,
        occurred_at=occurred_at or timezone.now(),
        location=_location(payload),
        accuracy_m=payload.get("accuracy_m"),
        payload_json=payload,
        idempotency_key=idempotency_key,
    )
    now = event.occurred_at
    if event_type == "ROUTE_STARTED":
        assert_transition(
            route.status,
            Route.Status.IN_PROGRESS,
            ROUTE_TRANSITIONS,
            entity="Route",
            error=DriverActionConflict,
        )
        route.status = Route.Status.IN_PROGRESS
        route.actual_start_at = now
        route.version += 1
        route.save(update_fields=["status", "actual_start_at", "version", "updated_at"])
        route.route_plan.status = RoutePlan.Status.IN_PROGRESS
        route.route_plan.version += 1
        route.route_plan.save(update_fields=["status", "version", "updated_at"])
        route.route_plan.planning_run.status = route.route_plan.planning_run.Status.IN_PROGRESS
        route.route_plan.planning_run.version += 1
        route.route_plan.planning_run.save(update_fields=["status", "version", "updated_at"])
        Driver.objects.filter(pk=driver.pk).update(status=Driver.Status.ON_ROUTE)
        Vehicle.objects.filter(pk=route.vehicle_id).update(status=Vehicle.Status.IN_USE)
        first = route.stops.filter(order__isnull=False, status=RouteStop.Status.PENDING).order_by("sequence").first()
        if first:
            first.status = RouteStop.Status.EN_ROUTE
            first.version += 1
            first.save(update_fields=["status", "version", "updated_at"])
    elif event_type == "STOP_ARRIVED":
        assert_transition(
            stop.status,
            RouteStop.Status.ARRIVED,
            STOP_TRANSITIONS,
            entity="Stop",
            error=DriverActionConflict,
        )
        stop.status = RouteStop.Status.ARRIVED
        stop.actual_arrival_at = now
        stop.version += 1
        stop.save(update_fields=["status", "actual_arrival_at", "version", "updated_at"])
        if stop.order_id:
            Order.objects.filter(pk=stop.order_id).update(
                status=Order.Status.IN_PROGRESS,
                version=F("version") + 1,
                updated_at=timezone.now(),
            )
    elif event_type in {"STOP_COMPLETED", "STOP_FAILED", "STOP_SKIPPED"}:
        target_status = {
            "STOP_COMPLETED": RouteStop.Status.COMPLETED,
            "STOP_FAILED": RouteStop.Status.FAILED,
            "STOP_SKIPPED": RouteStop.Status.SKIPPED,
        }[event_type]
        assert_transition(
            stop.status,
            target_status,
            STOP_TRANSITIONS,
            entity="Stop",
            error=DriverActionConflict,
        )
        stop.status = target_status
        stop.actual_departure_at = now
        stop.version += 1
        stop.save(update_fields=["status", "actual_departure_at", "version", "updated_at"])
        if stop.order_id:
            if event_type == "STOP_COMPLETED" and stop.stop_type != RouteStop.StopType.DELIVERY:
                order_status = Order.Status.IN_PROGRESS
            else:
                order_status = (
                    Order.Status.COMPLETED
                    if event_type == "STOP_COMPLETED"
                    else Order.Status.FAILED
                )
            Order.objects.filter(pk=stop.order_id).update(
                status=order_status,
                version=F("version") + 1,
                updated_at=timezone.now(),
            )
        if event_type != "STOP_COMPLETED":
            OperationException.objects.create(
                organization=driver.organization,
                order=stop.order,
                route=route,
                route_stop=stop,
                type=payload.get("reason_code", "FAILED_DELIVERY"),
                severity=OperationException.Severity.HIGH,
                description=payload.get("note", "Driver reported a failed or skipped stop."),
                reported_by=driver.user,
            )
        next_stop = route.stops.filter(sequence__gt=stop.sequence, order__isnull=False, status=RouteStop.Status.PENDING).first()
        if next_stop:
            next_stop.status = RouteStop.Status.EN_ROUTE
            next_stop.version += 1
            next_stop.save(update_fields=["status", "version", "updated_at"])
        elif not route.stops.filter(order__isnull=False).exclude(
            status__in=[RouteStop.Status.COMPLETED, RouteStop.Status.FAILED, RouteStop.Status.SKIPPED]
        ).exists():
            assert_transition(
                route.status,
                Route.Status.COMPLETED,
                ROUTE_TRANSITIONS,
                entity="Route",
                error=DriverActionConflict,
            )
            route.status = Route.Status.COMPLETED
            route.actual_end_at = now
            route.version += 1
            route.save(update_fields=["status", "actual_end_at", "version", "updated_at"])
            route_completed = True
            terminal_route_statuses = {Route.Status.COMPLETED, Route.Status.CANCELLED}
            plan = route.route_plan
            if not plan.routes.exclude(status__in=terminal_route_statuses).exists():
                assert_transition(
                    plan.status,
                    RoutePlan.Status.COMPLETED,
                    PLAN_TRANSITIONS,
                    entity="Route plan",
                    error=DriverActionConflict,
                )
                plan.status = RoutePlan.Status.COMPLETED
                plan.version += 1
                plan.save(update_fields=["status", "version", "updated_at"])
                run_status = plan.planning_run.Status.COMPLETED
                assert_transition(
                    plan.planning_run.status,
                    run_status,
                    RUN_TRANSITIONS,
                    entity="Planning run",
                    error=DriverActionConflict,
                )
                plan.planning_run.status = run_status
                plan.planning_run.version += 1
                plan.planning_run.save(update_fields=["status", "version", "updated_at"])
            if not Route.objects.filter(driver=driver, status=Route.Status.IN_PROGRESS).exists():
                Driver.objects.filter(pk=driver.pk).update(status=Driver.Status.AVAILABLE)
            if not Route.objects.filter(vehicle_id=route.vehicle_id, status=Route.Status.IN_PROGRESS).exists():
                Vehicle.objects.filter(pk=route.vehicle_id).update(status=Vehicle.Status.AVAILABLE)
    record_audit(
        organization=driver.organization,
        actor=driver.user,
        action=f"driver.{event_type.lower()}",
        instance=event,
        after=payload,
    )
    # Field events are the operational signal other systems care about most, so
    # proof of delivery and route completion are published on commit. Publishing
    # inside the transaction would announce work that a rollback then erases.
    organization = driver.organization
    published = []
    if event_type == "STOP_COMPLETED":
        published.append(("pod.captured", stop.pk if stop_id else None))
    if route_completed:
        published.append(("route.completed", route.pk))

    for name, reference in published:
        body = {
            # The idempotency key doubles as the stable identifier a subscriber
            # deduplicates on; DriverEvent has no separate event id.
            "idempotency_key": event.idempotency_key,
            "event_type": event_type,
            "route_id": route.pk,
            "route_status": route.status,
            "driver_id": driver.pk,
            "occurred_at": now.isoformat(),
        }
        if reference is not None:
            body["stop_id" if name == "pod.captured" else "route_id"] = reference
        transaction.on_commit(lambda n=name, b=body: emit_webhook_event(organization, n, b))
    return event, False


def record_location(*, driver, route, payload):
    try:
        point = _location(payload)
    except DriverActionConflict as exc:
        raise ValueError(str(exc)) from exc
    if not point:
        raise ValueError("longitude and latitude are required")
    recorded_at = payload.get("recorded_at") or timezone.now()
    if isinstance(recorded_at, str):
        recorded_at = parse_datetime(recorded_at)
        if recorded_at is None:
            raise ValueError("recorded_at must be an ISO-8601 timestamp")
    return VehicleLocation.objects.create(
        organization=driver.organization,
        vehicle=route.vehicle,
        driver=driver,
        route=route,
        recorded_at=recorded_at,
        location=point,
        speed_kph=payload.get("speed_kph"),
        heading=payload.get("heading"),
        accuracy_m=payload.get("accuracy_m"),
        source=payload.get("source", "driver_web"),
    )
