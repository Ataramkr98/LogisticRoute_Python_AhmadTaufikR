from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.gis.geos import LineString, Point
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.planning.models import (
    PlanningRun,
    Route,
    RoutePlan,
    RouteStop,
    UnassignedOrder,
)
from apps.routing.providers import Coordinate, get_router
from apps.routing.services import load_matrix, update_progress

from .solver import Node, SolverInput, SolverVehicle, solve_vrp


def _service_day_start(run):
    return timezone.make_aware(datetime.combine(run.service_date, time.min), ZoneInfo(run.depot.timezone))


# The solver objective is a single integer sum, so every term in it has to be
# expressed in one common unit. That unit is one metre of travel: the arc cost is
# already metres, and the two money-like terms (opening a vehicle, leaving an
# order unassigned) are converted into metres with the profile's exchange rate.
#
# ``objective_weights_json`` is a free-form dict, so the rate is read defensively
# and defaults to a value that keeps the terms in proportion. Without a shared
# unit a currency amount such as 150000 IDR drowns out travel costs in the
# thousands, and the solver then drops orders that are trivially servable simply
# because opening the vehicle looks more expensive than the drop penalty.
_DEFAULT_IDR_PER_METRE = 100


def _objective_units(weight_config):
    """Return the exchange rate that converts currency into objective units."""
    try:
        rate = float(weight_config.get("idr_per_objective_unit", _DEFAULT_IDR_PER_METRE))
    except (TypeError, ValueError):
        rate = _DEFAULT_IDR_PER_METRE
    return rate if rate > 0 else _DEFAULT_IDR_PER_METRE


def _vehicle_input(run, entry, base, units_per_currency):
    vehicle = entry.vehicle
    start_s, end_s = 0, min(172800, vehicle.max_route_duration_seconds)
    if entry.driver_id:
        shift = entry.driver.shifts.filter(shift_date=run.service_date).order_by("start_at").first()
        if shift:
            start_s = max(0, int((shift.start_at - base).total_seconds()))
            end_s = min(172800, int((shift.end_at - base).total_seconds()))
    return SolverVehicle(
        key=str(vehicle.pk),
        vehicle_type=vehicle.vehicle_type,
        capacity_weight_g=max(1, int(vehicle.capacity_weight_kg * 1000)),
        capacity_volume_l=max(1, int(vehicle.capacity_volume_m3 * 1000)),
        capacity_packages=max(1, vehicle.capacity_package_count),
        max_stops=vehicle.max_stops,
        max_duration_s=vehicle.max_route_duration_seconds,
        skills=frozenset(vehicle.skills_json),
        # Convert the vehicle's fixed cost into objective units so that opening a
        # vehicle is comparable with the metres it saves and with the penalty for
        # leaving an order unassigned.
        fixed_cost=max(0, int(float(vehicle.fixed_cost) / units_per_currency)),
        shift_window=(start_s, max(start_s + 1, end_s)),
    )


@transaction.atomic
def optimize_run(run_id):
    run = (
        PlanningRun.objects.select_for_update()
        .select_related("organization", "depot", "profile", "requested_by")
        .get(pk=run_id)
    )
    if run.status == PlanningRun.Status.CANCELLED:
        return None
    if run.status in {
        PlanningRun.Status.DISPATCHED,
        PlanningRun.Status.IN_PROGRESS,
        PlanningRun.Status.COMPLETED,
    }:
        raise ValueError(f"Planning run cannot be optimized from status {run.status}")
    if run.cancel_requested:
        run.status = PlanningRun.Status.CANCELLED
        run.save(update_fields=["status", "updated_at"])
        return None
    if not run.input_snapshot_json.get("matrix_id"):
        from apps.routing.services import build_matrix

        build_matrix(run.pk)
        run.refresh_from_db()
    run.status = PlanningRun.Status.OPTIMIZING
    run.started_at = run.started_at or timezone.now()
    run.save(update_fields=["status", "started_at", "updated_at"])
    update_progress(run, 60, "optimization", "Building solver constraints")
    payload = load_matrix(run)
    weight_config = {
        "unassigned": 1_000_000,
        "distance": 1,
        "duration": 1,
        **run.profile.objective_weights_json,
    }
    # Shared unit conversion: arc cost is metres, so both money-like weights are
    # normalised onto that same scale before they enter the objective.
    units_per_currency = _objective_units(weight_config)
    nodes = []
    for item in payload["nodes"]:
        penalty = None
        if item["order_id"] is not None:
            penalty = int(weight_config["unassigned"]) * max(1, 6 - int(item["priority"]))
        nodes.append(
            Node(
                key=item["key"],
                order_id=item["order_id"],
                stop_type=item["stop_type"],
                service_s=item["service_s"],
                weight_g=item["weight_g"],
                volume_l=item["volume_l"],
                packages=item["packages"],
                time_window=tuple(item["time_window"]) if item["time_window"] else None,
                required_skills=frozenset(item["required_skills"]),
                prohibited_vehicle_types=frozenset(item.get("prohibited_vehicle_types", [])),
                optional_penalty=penalty,
            )
        )
    vehicle_entries = list(run.run_vehicles.select_related("vehicle", "driver"))
    base = _service_day_start(run)
    vehicles = [_vehicle_input(run, entry, base, units_per_currency) for entry in vehicle_entries]
    result = solve_vrp(
        SolverInput(
            nodes=nodes,
            vehicles=vehicles,
            distances_m=payload["distances_m"],
            durations_s=payload["durations_s"],
            pickup_delivery_pairs=[tuple(pair) for pair in payload["pickup_delivery_pairs"]],
            time_limit_s=settings.SOLVER_TIME_LIMIT_SECONDS,
            distance_weight=int(weight_config["distance"]),
            duration_weight=int(weight_config["duration"]),
        )
    )
    update_progress(run, 85, "persistence", "Saving route plan and explanations")
    previous = run.route_plans.order_by("-plan_version").first()
    plan_version = previous.plan_version + 1 if previous else 1
    plan = RoutePlan.objects.create(
        organization=run.organization,
        planning_run=run,
        service_date=run.service_date,
        depot=run.depot,
        plan_version=plan_version,
        status=RoutePlan.Status.REVIEW,
        supersedes=previous,
        created_by=run.requested_by,
    )
    if previous and previous.status not in {RoutePlan.Status.DISPATCHED, RoutePlan.Status.COMPLETED}:
        previous.status = RoutePlan.Status.SUPERSEDED
        previous.save(update_fields=["status", "updated_at"])
    router = get_router()
    total_distance = 0
    total_duration = 0
    assigned_orders = set()
    for route_sequence, solved in enumerate(result.routes, start=1):
        entry = vehicle_entries[solved.vehicle_index]
        coordinate_points = [
            Coordinate(payload["nodes"][node_index]["longitude"], payload["nodes"][node_index]["latitude"])
            for node_index in solved.node_indices
        ]
        geometry_result = router.route(coordinate_points, run.profile.routing_profile)
        geometry = LineString(*geometry_result["coordinates"], srid=4326) if len(geometry_result["coordinates"]) >= 2 else None
        route = Route.objects.create(
            route_plan=plan,
            vehicle=entry.vehicle,
            driver=entry.driver,
            sequence=route_sequence,
            start_at_planned=base + timezone.timedelta(seconds=solved.arrivals_s[0]),
            end_at_planned=base + timezone.timedelta(seconds=solved.arrivals_s[-1]),
            total_distance_m=geometry_result["distance_m"] or solved.distance_m,
            total_duration_s=solved.duration_s,
            geometry=geometry,
            provider_route_id=geometry_result.get("provider_route_id", ""),
        )
        total_distance += route.total_distance_m
        total_duration += route.total_duration_s
        for stop_sequence, (node_index, arrival_s, load_weight_g, load_volume_l) in enumerate(
            zip(
                solved.node_indices,
                solved.arrivals_s,
                solved.load_weight_g,
                solved.load_volume_l,
                strict=True,
            ),
            start=1,
        ):
            node_data = payload["nodes"][node_index]
            stop_type = (
                RouteStop.StopType.DEPOT_START
                if stop_sequence == 1
                else RouteStop.StopType.DEPOT_END
                if stop_sequence == len(solved.node_indices)
                else node_data["stop_type"]
            )
            order_id = node_data["order_id"]
            stop = RouteStop.objects.create(
                route=route,
                order_id=order_id,
                stop_type=stop_type,
                sequence=stop_sequence,
                location=Point(node_data["longitude"], node_data["latitude"], srid=4326),
                planned_arrival_at=base + timezone.timedelta(seconds=arrival_s),
                planned_departure_at=base
                + timezone.timedelta(seconds=arrival_s + node_data["service_s"]),
                service_duration_s=node_data["service_s"],
                load_weight_after_kg=Decimal(load_weight_g) / 1000,
                load_volume_after_m3=Decimal(load_volume_l) / 1000,
            )
            if order_id:
                assigned_orders.add(order_id)
                if node_data["stop_type"] == "DELIVERY":
                    from apps.orders.models import Order

                    Order.objects.filter(pk=order_id).update(
                        assigned_route_stop=stop,
                        status=Order.Status.PLANNED,
                        version=F("version") + 1,
                        updated_at=timezone.now(),
                    )
    all_order_ids = {item["order_id"] for item in payload["nodes"] if item["order_id"]}
    for order_id in sorted(all_order_ids - assigned_orders):
        UnassignedOrder.objects.create(
            route_plan=plan,
            order_id=order_id,
            reason_code="NO_FEASIBLE_ASSIGNMENT",
            explanation="No vehicle could serve this order within all active hard constraints.",
            violated_constraints_json=["capacity", "time_window", "skills", "availability"],
        )
        from apps.orders.models import Order

        Order.objects.filter(pk=order_id).update(
            status=Order.Status.UNASSIGNED,
            assigned_route_stop=None,
            version=F("version") + 1,
            updated_at=timezone.now(),
        )
    run.status = PlanningRun.Status.REVIEW
    run.completed_at = timezone.now()
    run.objective_score = result.objective
    run.solver_version = result.solver_version
    run.total_distance_m = total_distance
    run.total_duration_s = total_duration
    run.vehicles_used = len(result.routes)
    run.unassigned_count = len(all_order_ids - assigned_orders)
    run.metrics_json = {"solver_status": result.status, "route_count": len(result.routes)}
    run.progress_percent = 100
    run.current_phase = "complete"
    run.progress_message = f"{result.status}: {len(result.routes)} routes generated"
    run.save()
    update_progress(run, 100, "complete", run.progress_message)
    return plan
