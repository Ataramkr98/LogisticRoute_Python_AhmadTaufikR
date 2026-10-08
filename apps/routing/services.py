import gzip
import hashlib
import json
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.common.events import publish
from apps.customers.models import Address, GeocodeCache
from apps.planning.models import DistanceMatrix, PlanningRun

from .providers import Coordinate, ProviderError, get_geocoder, get_router


def _normalized_address(address):
    return " ".join(address.one_line.casefold().split())


def geocode_address(address: Address):
    geocoder = get_geocoder()
    normalized_hash = hashlib.sha256(_normalized_address(address).encode()).hexdigest()
    cached = GeocodeCache.objects.filter(
        normalized_hash=normalized_hash,
        provider=geocoder.name,
        expires_at__gt=timezone.now(),
    ).first()
    if cached:
        result_point = cached.location
        formatted = cached.formatted_address
        confidence = cached.confidence
    else:
        result = geocoder.geocode(address.one_line)
        result_point = Point(result.coordinate.longitude, result.coordinate.latitude, srid=4326)
        formatted = result.formatted_address
        confidence = Decimal(str(result.confidence))
        GeocodeCache.objects.update_or_create(
            normalized_hash=normalized_hash,
            provider=geocoder.name,
            defaults={
                "formatted_address": formatted,
                "location": result_point,
                "confidence": confidence,
                "raw_response": result.raw,
                "expires_at": timezone.now() + timezone.timedelta(days=30),
            },
        )
    address.location = result_point
    address.formatted_address = formatted
    address.geocode_confidence = confidence
    address.geocode_provider = geocoder.name
    address.geocode_status = Address.GeocodeStatus.VALID if confidence >= Decimal("0.6") else Address.GeocodeStatus.AMBIGUOUS
    address.save(
        update_fields=[
            "location",
            "formatted_address",
            "geocode_confidence",
            "geocode_provider",
            "geocode_status",
            "updated_at",
        ]
    )
    return address


def build_node_snapshot(run: PlanningRun):
    depot = run.depot
    restricted_zones = list(run.organization.restricted_zones.filter(active=True))

    def zone_constraints(location):
        prohibited = set()
        required = set()
        for zone in restricted_zones:
            if zone.polygon.contains(location) or zone.polygon.touches(location):
                prohibited.update(zone.prohibited_vehicle_types)
                required.update(zone.required_skills)
        return prohibited, required

    nodes = [
        {
            "key": "depot",
            "order_id": None,
            "stop_type": "DEPOT",
            "longitude": depot.location.x,
            "latitude": depot.location.y,
            "service_s": 0,
            "weight_g": 0,
            "volume_l": 0,
            "packages": 0,
            "required_skills": [],
            "prohibited_vehicle_types": [],
            "time_window": None,
            "priority": 0,
        }
    ]
    pairs = []
    base = timezone.make_aware(datetime.combine(run.service_date, time.min), ZoneInfo(depot.timezone))
    entries = run.run_orders.select_related(
        "order__pickup_address", "order__delivery_address"
    ).filter(eligibility_status="ELIGIBLE")
    for entry in entries:
        order = entry.order
        delivery = order.delivery_address
        if not delivery.location:
            raise ProviderError(f"Order {order.external_ref} has no geocoded delivery address")
        window = None
        if order.time_window_start and order.time_window_end:
            window = [
                max(0, int((order.time_window_start - base).total_seconds())),
                min(172800, int((order.time_window_end - base).total_seconds())),
            ]
        demand = {
            "weight_g": int(order.demand_weight_kg * 1000),
            "volume_l": int(order.demand_volume_m3 * 1000),
            "packages": order.package_count,
        }
        pickup_index = None
        if order.pickup_address_id:
            pickup = order.pickup_address
            if not pickup.location:
                raise ProviderError(f"Order {order.external_ref} has no geocoded pickup address")
            pickup_prohibited, pickup_required = zone_constraints(pickup.location)
            pickup_index = len(nodes)
            nodes.append(
                {
                    "key": f"order:{order.pk}:pickup",
                    "order_id": order.pk,
                    "stop_type": "PICKUP",
                    "longitude": pickup.location.x,
                    "latitude": pickup.location.y,
                    "service_s": order.service_duration_seconds,
                    **demand,
                    "required_skills": sorted(set(order.required_skills_json) | pickup_required),
                    "prohibited_vehicle_types": sorted(pickup_prohibited),
                    "time_window": window,
                    "priority": order.priority,
                }
            )
        delivery_prohibited, delivery_required = zone_constraints(delivery.location)
        delivery_index = len(nodes)
        nodes.append(
            {
                "key": f"order:{order.pk}:delivery",
                "order_id": order.pk,
                "stop_type": "DELIVERY",
                "longitude": delivery.location.x,
                "latitude": delivery.location.y,
                "service_s": order.service_duration_seconds,
                "weight_g": -demand["weight_g"] if pickup_index is not None else demand["weight_g"],
                "volume_l": -demand["volume_l"] if pickup_index is not None else demand["volume_l"],
                "packages": -demand["packages"] if pickup_index is not None else demand["packages"],
                "required_skills": sorted(set(order.required_skills_json) | delivery_required),
                "prohibited_vehicle_types": sorted(delivery_prohibited),
                "time_window": window,
                "priority": order.priority,
            }
        )
        if pickup_index is not None:
            pairs.append([pickup_index, delivery_index])
    return nodes, pairs


def update_progress(run, percent, phase, message):
    run.progress_percent = percent
    run.current_phase = phase
    run.progress_message = message
    run.version += 1
    run.save(update_fields=["progress_percent", "current_phase", "progress_message", "version", "updated_at"])
    publish(
        f"planning:{run.pk}",
        {
            "planning_run_id": run.pk,
            "version": run.version,
            "status": run.status,
            "percent": percent,
            "phase": phase,
            "message": message,
        },
    )


@transaction.atomic
def build_matrix(run_id):
    run = PlanningRun.objects.select_for_update().select_related("depot", "profile", "organization").get(pk=run_id)
    if run.status == PlanningRun.Status.CANCELLED:
        return None
    if run.status in {
        PlanningRun.Status.DISPATCHED,
        PlanningRun.Status.IN_PROGRESS,
        PlanningRun.Status.COMPLETED,
    }:
        raise ValueError(f"Planning matrix cannot be rebuilt from status {run.status}")
    if run.cancel_requested:
        run.status = PlanningRun.Status.CANCELLED
        run.save(update_fields=["status", "updated_at"])
        return None
    run.status = PlanningRun.Status.MATRIX_PENDING
    run.started_at = run.started_at or timezone.now()
    run.save(update_fields=["status", "started_at", "updated_at"])
    update_progress(run, 10, "matrix", "Preparing geocoded locations")
    nodes, pairs = build_node_snapshot(run)
    if len(nodes) - 1 > settings.MAX_ORDERS_PER_RUN * 2:
        raise ValueError("Planning run exceeds MAX_ORDERS_PER_RUN")
    key_material = {
        "provider": settings.ROUTING_PROVIDER,
        "profile": run.profile.routing_profile,
        "points": [[node["longitude"], node["latitude"]] for node in nodes],
    }
    matrix_key = hashlib.sha256(json.dumps(key_material, sort_keys=True).encode()).hexdigest()
    cached = DistanceMatrix.objects.filter(
        organization=run.organization,
        matrix_key=matrix_key,
        expires_at__gt=timezone.now(),
    ).first()
    if cached and default_storage.exists(cached.matrix_storage_key):
        matrix_record = cached
    else:
        update_progress(run, 30, "matrix", f"Requesting {len(nodes)} × {len(nodes)} travel matrix")
        router = get_router()
        result = router.matrix(
            [Coordinate(node["longitude"], node["latitude"]) for node in nodes],
            run.profile.routing_profile,
        )
        payload = {
            "nodes": nodes,
            "pickup_delivery_pairs": pairs,
            "distances_m": result.distances_m,
            "durations_s": result.durations_s,
            "provider": result.provider,
        }
        content = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
        storage_key = default_storage.save(f"matrices/{run.organization_id}/{matrix_key}.json.gz", ContentFile(content))
        matrix_record, _ = DistanceMatrix.objects.update_or_create(
            organization=run.organization,
            matrix_key=matrix_key,
            defaults={
                "provider": result.provider,
                "profile": run.profile.routing_profile,
                "location_count": len(nodes),
                "matrix_storage_key": storage_key,
                "expires_at": timezone.now() + timezone.timedelta(hours=24),
            },
        )
    run.matrix_provider = matrix_record.provider
    run.input_hash = matrix_key
    run.input_snapshot_json = {"matrix_id": matrix_record.pk, "nodes": nodes, "pickup_delivery_pairs": pairs}
    run.save(update_fields=["matrix_provider", "input_hash", "input_snapshot_json", "updated_at"])
    update_progress(run, 50, "matrix", "Travel matrix is ready")
    return matrix_record


def load_matrix(run):
    matrix_id = run.input_snapshot_json.get("matrix_id")
    matrix = DistanceMatrix.objects.get(pk=matrix_id, organization=run.organization)
    with default_storage.open(matrix.matrix_storage_key, "rb") as file:
        payload = json.loads(gzip.decompress(file.read()))
    current_nodes = run.input_snapshot_json.get("nodes", [])
    if len(payload.get("distances_m", [])) != len(current_nodes):
        raise ProviderError("Cached matrix dimensions do not match the planning snapshot")
    payload["nodes"] = current_nodes
    payload["pickup_delivery_pairs"] = run.input_snapshot_json.get(
        "pickup_delivery_pairs",
        [],
    )
    return payload
