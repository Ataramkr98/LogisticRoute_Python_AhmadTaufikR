from copy import deepcopy

from django.contrib.gis.geos import LineString
from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone

from apps.audit.services import record_audit
from apps.orders.models import Order
from apps.planning.models import Route, RoutePlan, RouteStop, RouteViolation
from apps.routing.providers import Coordinate, get_router


class PlanConflict(ValueError):
    pass


def _copy_model(instance, overrides=None):
    values = {}
    for field in instance._meta.concrete_fields:
        if field.primary_key or field.auto_created:
            continue
        values[field.attname] = deepcopy(getattr(instance, field.attname))
    values.update(overrides or {})
    return instance.__class__.objects.create(**values)


@transaction.atomic
def clone_plan(plan, actor, expected_version=None):
    plan = RoutePlan.objects.select_for_update().get(pk=plan.pk)
    if expected_version is not None and plan.version != int(expected_version):
        raise PlanConflict(
            f"Version conflict: expected {expected_version}, current version is {plan.version}"
        )
    if plan.status not in {RoutePlan.Status.DRAFT, RoutePlan.Status.OPTIMIZED, RoutePlan.Status.REVIEW}:
        raise PlanConflict("Only a draft or review plan can be cloned")
    next_version = (
        RoutePlan.objects.filter(planning_run=plan.planning_run).aggregate(value=Max("plan_version"))["value"] or 0
    ) + 1
    cloned = _copy_model(
        plan,
        {
            "plan_version": next_version,
            "version": 1,
            "status": RoutePlan.Status.REVIEW,
            "supersedes_id": plan.pk,
            "created_by_id": actor.pk,
            "dispatched_at": None,
        },
    )
    route_map = {}
    stop_map = {}
    for route in plan.routes.all():
        new_route = _copy_model(
            route,
            {
                "route_plan_id": cloned.pk,
                "version": 1,
                "status": Route.Status.DRAFT,
                "actual_start_at": None,
                "actual_end_at": None,
            },
        )
        route_map[route.pk] = new_route
        for stop in route.stops.all():
            new_stop = _copy_model(
                stop,
                {
                    "route_id": new_route.pk,
                    "version": 1,
                    "status": RouteStop.Status.PENDING,
                    "actual_arrival_at": None,
                    "actual_departure_at": None,
                },
            )
            stop_map[stop.pk] = new_stop
            if new_stop.order_id and new_stop.stop_type == RouteStop.StopType.DELIVERY:
                Order.objects.filter(pk=new_stop.order_id).update(
                    assigned_route_stop=new_stop,
                    version=F("version") + 1,
                    updated_at=timezone.now(),
                )
    plan.status = RoutePlan.Status.SUPERSEDED
    plan.version += 1
    plan.save(update_fields=["status", "version", "updated_at"])
    record_audit(
        organization=plan.organization,
        actor=actor,
        action="route_plan.cloned",
        instance=cloned,
        after={"supersedes": plan.pk, "plan_version": next_version},
    )
    return cloned, route_map, stop_map


def recalculate_route(route):
    stops = list(route.stops.order_by("sequence"))
    if len(stops) < 2:
        return route
    router = get_router()
    points = [Coordinate(stop.location.x, stop.location.y) for stop in stops]
    matrix = router.matrix(points, route.route_plan.planning_run.profile.routing_profile)
    geometry_data = router.route(points, route.route_plan.planning_run.profile.routing_profile)
    current = route.start_at_planned
    distance = 0
    waiting = 0
    for index, stop in enumerate(stops):
        if index:
            current += timezone.timedelta(seconds=matrix.durations_s[index - 1][index])
            distance += matrix.distances_m[index - 1][index]
        if stop.order_id and stop.order.time_window_start and current < stop.order.time_window_start:
            wait = int((stop.order.time_window_start - current).total_seconds())
            waiting += wait
            current = stop.order.time_window_start
        stop.planned_arrival_at = current
        current += timezone.timedelta(seconds=stop.service_duration_s)
        stop.planned_departure_at = current
        stop.save(update_fields=["planned_arrival_at", "planned_departure_at", "updated_at"])
    route.end_at_planned = current
    route.total_distance_m = geometry_data["distance_m"] or distance
    route.total_duration_s = int((current - route.start_at_planned).total_seconds())
    route.total_waiting_s = waiting
    coordinates = geometry_data["coordinates"]
    route.geometry = LineString(*coordinates, srid=4326) if len(coordinates) >= 2 else None
    route.version += 1
    route.save(
        update_fields=[
            "end_at_planned",
            "total_distance_m",
            "total_duration_s",
            "total_waiting_s",
            "geometry",
            "version",
            "updated_at",
        ]
    )
    return route


def validate_plan(plan):
    plan.violations.filter(acknowledged_at__isnull=True).delete()
    violations = []
    restricted_zones = list(plan.organization.restricted_zones.filter(active=True))
    seen_vehicle_ids = set()
    seen_driver_ids = set()
    for route in plan.routes.select_related("vehicle", "driver").prefetch_related("stops__order"):
        vehicle = route.vehicle
        stops = list(route.stops.order_by("sequence"))
        order_stops = [stop for stop in stops if stop.order_id]
        if len(order_stops) > vehicle.max_stops:
            violations.append(("MAX_STOPS", f"{route} exceeds maximum stops", route, None))
        total_weight = sum((stop.order.demand_weight_kg for stop in order_stops if stop.stop_type == "DELIVERY"), 0)
        total_volume = sum((stop.order.demand_volume_m3 for stop in order_stops if stop.stop_type == "DELIVERY"), 0)
        total_packages = sum((stop.order.package_count for stop in order_stops if stop.stop_type == "DELIVERY"), 0)
        if total_weight > vehicle.capacity_weight_kg:
            violations.append(("WEIGHT_CAPACITY", "Weight capacity exceeded", route, None))
        if total_volume > vehicle.capacity_volume_m3:
            violations.append(("VOLUME_CAPACITY", "Volume capacity exceeded", route, None))
        if total_packages > vehicle.capacity_package_count:
            violations.append(("PACKAGE_CAPACITY", "Package capacity exceeded", route, None))
        if route.total_duration_s > vehicle.max_route_duration_seconds:
            violations.append(("MAX_DURATION", "Maximum route duration exceeded", route, None))
        if not route.driver_id:
            violations.append(("DRIVER_REQUIRED", "A driver must be assigned before dispatch", route, None))
        elif route.driver_id in seen_driver_ids:
            violations.append(("DRIVER_DUPLICATE", "Driver is assigned to more than one route", route, None))
        else:
            seen_driver_ids.add(route.driver_id)
        if route.vehicle_id in seen_vehicle_ids:
            violations.append(("VEHICLE_DUPLICATE", "Vehicle is assigned to more than one route", route, None))
        else:
            seen_vehicle_ids.add(route.vehicle_id)
        route_skills = set(vehicle.skills_json)
        for stop in order_stops:
            required_skills = set(stop.order.required_skills_json)
            prohibited_vehicle_types = set()
            for zone in restricted_zones:
                if zone.polygon.contains(stop.location) or zone.polygon.touches(stop.location):
                    required_skills.update(zone.required_skills)
                    prohibited_vehicle_types.update(zone.prohibited_vehicle_types)
            missing = required_skills - route_skills
            if missing:
                violations.append(("SKILL", f"Missing skills: {', '.join(sorted(missing))}", route, stop))
            if vehicle.vehicle_type in prohibited_vehicle_types:
                violations.append(
                    (
                        "RESTRICTED_ZONE",
                        f"Vehicle type {vehicle.vehicle_type} is prohibited at this stop",
                        route,
                        stop,
                    )
                )
            if stop.order.time_window_end and stop.planned_arrival_at > stop.order.time_window_end:
                violations.append(("TIME_WINDOW", "Planned arrival is after the time window", route, stop))
        positions = {}
        for index, stop in enumerate(order_stops):
            positions.setdefault(stop.order_id, {})[stop.stop_type] = index
        for order_id, position in positions.items():
            if "PICKUP" in position and "DELIVERY" in position and position["PICKUP"] >= position["DELIVERY"]:
                stop = next(s for s in order_stops if s.order_id == order_id)
                violations.append(("PRECEDENCE", "Pickup must occur before delivery", route, stop))
    for constraint_type, message, route, stop in violations:
        RouteViolation.objects.create(
            route_plan=plan,
            route=route,
            route_stop=stop,
            severity=RouteViolation.Severity.ERROR,
            constraint_type=constraint_type,
            message=message,
            details_json={},
        )
    return plan.violations.all()


@transaction.atomic
def move_stop(*, plan, stop_id, target_route_id, target_sequence, actor):
    cloned, route_map, stop_map = clone_plan(plan, actor)
    old_stop = RouteStop.objects.select_related("route").get(pk=stop_id, route__route_plan=plan)
    if old_stop.locked or old_stop.stop_type in {RouteStop.StopType.DEPOT_START, RouteStop.StopType.DEPOT_END}:
        raise PlanConflict("This stop cannot be moved")
    stop = stop_map[old_stop.pk]
    target_route = route_map.get(target_route_id)
    if not target_route:
        raise PlanConflict("Target route does not belong to the plan")
    affected = {stop.route, target_route}
    stop.sequence = 1_000_000
    stop.save(update_fields=["sequence"])
    stop.route = target_route
    stop.save(update_fields=["route"])
    for route in affected:
        ordered = list(route.stops.exclude(pk=stop.pk).order_by("sequence"))
        insert_at = max(1, min(target_sequence - 1, max(1, len(ordered) - 1)))
        if route.pk == target_route.pk:
            ordered.insert(insert_at, stop)
        route.stops.exclude(pk=stop.pk).update(sequence=F("sequence") + 100000)
        for sequence, item in enumerate(ordered, start=1):
            item.sequence = sequence
            item.save(update_fields=["sequence"])
        recalculate_route(route)
    validate_plan(cloned)
    record_audit(
        organization=cloned.organization,
        actor=actor,
        action="route_stop.moved",
        instance=stop,
        after={"route_id": target_route.pk, "sequence": stop.sequence},
    )
    return cloned
