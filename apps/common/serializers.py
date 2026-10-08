import json
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.gis.geos import Point, Polygon
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from apps.common.scoping import membership_depot_ids
from apps.customers.models import Address, Customer
from apps.depots.models import Depot, RestrictedZone
from apps.drivers.models import Driver, DriverDevice, DriverShift
from apps.exceptions.models import OperationException
from apps.fleet.models import Vehicle, VehicleAvailability
from apps.integrations.models import WebhookSubscription
from apps.integrations.services import EVENT_TYPES
from apps.integrations.validation import validate_webhook_url
from apps.orders.models import ImportJob, Order, OrderItem, OrderTimeWindow
from apps.planning.models import (
    PlanningProfile,
    PlanningRun,
    PlanningRunOrder,
    PlanningRunVehicle,
    Route,
    RoutePlan,
    RouteStop,
    RouteViolation,
    UnassignedOrder,
)
from apps.proof_of_delivery.models import ProofOfDelivery
from apps.reports.models import ReportExport
from apps.tracking.models import DriverEvent, VehicleLocation


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "longitude": {"type": "number", "format": "double"},
            "latitude": {"type": "number", "format": "double"},
        },
        "required": ["longitude", "latitude"],
        "nullable": True,
    }
)
class CoordinateField(serializers.Field):
    def to_representation(self, value):
        if not value:
            return None
        return {"longitude": value.x, "latitude": value.y}

    def to_internal_value(self, data):
        if data in (None, "") and self.allow_null:
            return None
        if not isinstance(data, dict) or "longitude" not in data or "latitude" not in data:
            raise serializers.ValidationError("Use {longitude, latitude}.")
        try:
            longitude, latitude = float(data["longitude"]), float(data["latitude"])
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("Coordinates must be numeric.") from exc
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise serializers.ValidationError("Coordinates are outside WGS84 bounds.")
        return Point(longitude, latitude, srid=4326)


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["Polygon"]},
            "coordinates": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "number", "format": "double"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
            },
        },
        "required": ["type", "coordinates"],
        "nullable": True,
    }
)
class PolygonGeoJSONField(serializers.Field):
    def to_representation(self, value):
        if not value:
            return None
        return json.loads(value.geojson)

    def to_internal_value(self, data):
        if data in (None, "") and self.allow_null:
            return None
        if not isinstance(data, dict) or data.get("type") != "Polygon":
            raise serializers.ValidationError("Use a GeoJSON Polygon object.")
        coordinates = data.get("coordinates")
        if not isinstance(coordinates, list) or not coordinates:
            raise serializers.ValidationError("Polygon coordinates require at least one ring.")
        rings = []
        try:
            for ring in coordinates:
                points = [(float(longitude), float(latitude)) for longitude, latitude in ring]
                if len(points) < 4 or points[0] != points[-1]:
                    raise serializers.ValidationError(
                        "Every polygon ring must be closed and contain at least four positions."
                    )
                if any(
                    not -180 <= longitude <= 180 or not -90 <= latitude <= 90
                    for longitude, latitude in points
                ):
                    raise serializers.ValidationError("Polygon coordinates are outside WGS84 bounds.")
                rings.append(points)
        except serializers.ValidationError:
            raise
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("Polygon coordinates must be numeric positions.") from exc
        try:
            return Polygon(*rings, srid=4326)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("Polygon geometry is invalid.") from exc


class OrganizationScopedSerializer(serializers.ModelSerializer):
    @property
    def organization(self):
        return self.context["request"].organization

    def create(self, validated_data):
        validated_data.setdefault("organization", self.organization)
        return super().create(validated_data)


def _instance_value(serializer, attrs, field_name):
    return attrs.get(field_name, getattr(serializer.instance, field_name, None))


def _validate_organization(organization, **objects):
    errors = {}
    for field_name, instance in objects.items():
        if instance is not None and getattr(instance, "organization_id", None) != organization.pk:
            errors[field_name] = "Must belong to the active organization."
    if errors:
        raise serializers.ValidationError(errors)


def _validate_depot_access(request, **depots):
    """Reject any depot outside the caller's authorized scope.

    ``membership_depot_ids`` returns ``None`` only when there is no membership,
    which the permission layer already rejected. An empty list is a real
    membership scoped to zero depots, so every depot fails the check â€” treating
    it as unrestricted would hand a depot-less member full write access.
    """
    allowed_ids = membership_depot_ids(request)
    if allowed_ids is None:
        return
    errors = {
        field_name: "Is outside your authorized depot scope."
        for field_name, depot in depots.items()
        if depot is not None and depot.pk not in allowed_ids
    }
    if errors:
        raise serializers.ValidationError(errors)


class DepotSerializer(OrganizationScopedSerializer):
    location = CoordinateField()
    service_area = PolygonGeoJSONField(required=False, allow_null=True)

    class Meta:
        model = Depot
        exclude = ["organization"]
        read_only_fields = ["version"] if hasattr(Depot, "version") else []

    def validate_timezone(self, value):
        try:
            ZoneInfo(value)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise serializers.ValidationError("Use a valid IANA time zone name.") from exc
        return value


class RestrictedZoneSerializer(OrganizationScopedSerializer):
    polygon = PolygonGeoJSONField()

    class Meta:
        model = RestrictedZone
        exclude = ["organization"]


class CustomerSerializer(OrganizationScopedSerializer):
    class Meta:
        model = Customer
        exclude = ["organization"]


class AddressSerializer(OrganizationScopedSerializer):
    location = CoordinateField(required=False, allow_null=True)

    class Meta:
        model = Address
        exclude = ["organization"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        _validate_organization(
            self.organization,
            customer=_instance_value(self, attrs, "customer"),
        )
        return attrs


class VehicleSerializer(OrganizationScopedSerializer):
    start_location = CoordinateField()
    end_location = CoordinateField()

    class Meta:
        model = Vehicle
        exclude = ["organization"]
        read_only_fields = ["version"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        _validate_organization(
            self.organization,
            depot=_instance_value(self, attrs, "depot"),
        )
        _validate_depot_access(
            self.context["request"],
            depot=_instance_value(self, attrs, "depot"),
        )
        return attrs


class VehicleAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleAvailability
        fields = "__all__"


class DriverSerializer(OrganizationScopedSerializer):
    class Meta:
        model = Driver
        exclude = ["organization"]
        read_only_fields = ["version"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        depot = _instance_value(self, attrs, "depot")
        user = _instance_value(self, attrs, "user")
        _validate_organization(self.organization, depot=depot)
        _validate_depot_access(self.context["request"], depot=depot)
        if user and not user.organization_memberships.filter(
            organization=self.organization,
            active=True,
        ).exists():
            raise serializers.ValidationError(
                {"user": "Must have an active membership in the organization."}
            )
        return attrs


class DriverShiftSerializer(serializers.ModelSerializer):
    start_location = CoordinateField(required=False, allow_null=True)
    end_location = CoordinateField(required=False, allow_null=True)

    class Meta:
        model = DriverShift
        fields = "__all__"

    def validate(self, attrs):
        attrs = super().validate(attrs)
        driver = _instance_value(self, attrs, "driver")
        organization = self.context["request"].organization
        _validate_organization(organization, driver=driver)
        _validate_depot_access(self.context["request"], driver=driver.depot if driver else None)
        start_at = _instance_value(self, attrs, "start_at")
        end_at = _instance_value(self, attrs, "end_at")
        if start_at and end_at and end_at <= start_at:
            raise serializers.ValidationError({"end_at": "Must be after the start."})
        return attrs


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        exclude = ["order"]


class OrderTimeWindowSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderTimeWindow
        exclude = ["order"]

    def validate(self, attrs):
        if attrs["end_at"] <= attrs["start_at"]:
            raise serializers.ValidationError({"end_at": "Must be after the start."})
        return attrs


class OrderSerializer(OrganizationScopedSerializer):
    items = OrderItemSerializer(many=True, required=False)
    time_windows = OrderTimeWindowSerializer(many=True, required=False)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    delivery_address_text = serializers.CharField(source="delivery_address.one_line", read_only=True)

    class Meta:
        model = Order
        exclude = ["organization", "assigned_route_stop"]
        read_only_fields = ["created_by", "status", "version", "cancelled_at"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        start = attrs.get("time_window_start", getattr(self.instance, "time_window_start", None))
        end = attrs.get("time_window_end", getattr(self.instance, "time_window_end", None))
        if start and end and end <= start:
            raise serializers.ValidationError({"time_window_end": "Must be after the start."})
        order_type = attrs.get("order_type", getattr(self.instance, "order_type", None))
        if order_type == Order.Type.PICKUP_DELIVERY and not attrs.get(
            "pickup_address", getattr(self.instance, "pickup_address", None)
        ):
            raise serializers.ValidationError({"pickup_address": "Pickup address is required."})
        relationships = {
            field_name: _instance_value(self, attrs, field_name)
            for field_name in (
                "depot",
                "customer",
                "pickup_address",
                "delivery_address",
                "preferred_driver",
            )
        }
        _validate_organization(self.organization, **relationships)
        _validate_depot_access(self.context["request"], depot=relationships["depot"])
        customer = relationships["customer"]
        for field_name in ("pickup_address", "delivery_address"):
            address = relationships[field_name]
            if customer and address and address.customer_id and address.customer_id != customer.pk:
                raise serializers.ValidationError(
                    {field_name: "Must belong to the selected customer."}
                )
        return attrs

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        windows = validated_data.pop("time_windows", [])
        validated_data["created_by"] = self.context["request"].user
        pickup = validated_data.get("pickup_address")
        delivery = validated_data.get("delivery_address")
        needs_pickup = validated_data.get("order_type") == Order.Type.PICKUP_DELIVERY
        if delivery and delivery.location and (not needs_pickup or (pickup and pickup.location)):
            validated_data["status"] = Order.Status.READY
        order = super().create(validated_data)
        OrderItem.objects.bulk_create([OrderItem(order=order, **item) for item in items])
        OrderTimeWindow.objects.bulk_create([OrderTimeWindow(order=order, **window) for window in windows])
        return order

    @transaction.atomic
    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)
        windows = validated_data.pop("time_windows", None)
        order = super().update(instance, validated_data)
        if items is not None:
            order.items.all().delete()
            OrderItem.objects.bulk_create([OrderItem(order=order, **item) for item in items])
        if windows is not None:
            order.time_windows.all().delete()
            OrderTimeWindow.objects.bulk_create(
                [OrderTimeWindow(order=order, **window) for window in windows]
            )
        return order


class ImportJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportJob
        fields = "__all__"
        read_only_fields = [
            "organization",
            "status",
            "total_rows",
            "processed_rows",
            "accepted_rows",
            "rejected_rows",
            "requested_by",
            "error_message",
        ]

    def validate(self, attrs):
        organization = self.context["request"].organization
        _validate_organization(organization, depot=attrs.get("depot"))
        _validate_depot_access(self.context["request"], depot=attrs.get("depot"))
        source = attrs.get("source_file")
        if source and not source.name.lower().endswith(".csv"):
            raise serializers.ValidationError({"source_file": "Choose a UTF-8 CSV file."})
        if source and source.size > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
            raise serializers.ValidationError({"source_file": "The CSV file is too large."})
        return attrs


class PlanningProfileSerializer(OrganizationScopedSerializer):
    class Meta:
        model = PlanningProfile
        exclude = ["organization"]
        read_only_fields = ["version"]


class PlanningVehicleSelectionSerializer(serializers.Serializer):
    vehicle_id = serializers.IntegerField(min_value=1)
    driver_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    locked = serializers.BooleanField(required=False, default=False)


class PlanningRunCreateSerializer(serializers.Serializer):
    depot_id = serializers.PrimaryKeyRelatedField(source="depot", queryset=Depot.objects.all())
    service_date = serializers.DateField()
    profile_id = serializers.PrimaryKeyRelatedField(source="profile", queryset=PlanningProfile.objects.all())
    order_ids = serializers.PrimaryKeyRelatedField(queryset=Order.objects.all(), many=True)
    vehicles = PlanningVehicleSelectionSerializer(many=True, allow_empty=False)

    def validate(self, attrs):
        organization = self.context["request"].organization
        if attrs["depot"].organization_id != organization.id or attrs["profile"].organization_id != organization.id:
            raise serializers.ValidationError("Depot and profile must belong to the active organization.")
        if not attrs["depot"].active or not attrs["profile"].active:
            raise serializers.ValidationError("Depot and profile must be active.")
        _validate_depot_access(self.context["request"], depot=attrs["depot"])
        if any(order.organization_id != organization.id for order in attrs["order_ids"]):
            raise serializers.ValidationError("Every order must belong to the active organization.")
        if len(attrs["order_ids"]) > settings.MAX_ORDERS_PER_RUN:
            raise serializers.ValidationError(
                {"order_ids": f"At most {settings.MAX_ORDERS_PER_RUN} orders can be planned at once."}
            )
        ineligible_orders = [
            order.pk
            for order in attrs["order_ids"]
            if order.depot_id != attrs["depot"].pk
            or order.service_date != attrs["service_date"]
            or order.status not in {Order.Status.READY, Order.Status.UNASSIGNED}
            or not order.delivery_address.location
            or (
                order.order_type == Order.Type.PICKUP_DELIVERY
                and (not order.pickup_address or not order.pickup_address.location)
            )
        ]
        if ineligible_orders:
            raise serializers.ValidationError(
                {"order_ids": f"Ineligible order IDs: {ineligible_orders}"}
            )
        selected_vehicle_ids = [selection.get("vehicle_id") for selection in attrs["vehicles"]]
        if any(value is None for value in selected_vehicle_ids):
            raise serializers.ValidationError({"vehicles": "Every selection requires vehicle_id."})
        available = Vehicle.objects.filter(
            organization=organization,
            pk__in=selected_vehicle_ids,
            active=True,
            status=Vehicle.Status.AVAILABLE,
            depot=attrs["depot"],
        )
        if available.count() != len(set(selected_vehicle_ids)):
            raise serializers.ValidationError({"vehicles": "A selected vehicle is unavailable or belongs to another depot."})
        blocked_ids = available.filter(
            availability_periods__status="UNAVAILABLE",
            availability_periods__start_at__date__lte=attrs["service_date"],
            availability_periods__end_at__date__gte=attrs["service_date"],
        ).values_list("pk", flat=True)
        if blocked_ids:
            raise serializers.ValidationError({"vehicles": f"Unavailable vehicle IDs: {list(blocked_ids)}"})
        selected_driver_ids = [selection.get("driver_id") for selection in attrs["vehicles"]]
        selected_driver_ids = [value for value in selected_driver_ids if value is not None]
        if len(selected_driver_ids) != len(set(selected_driver_ids)):
            raise serializers.ValidationError(
                {"vehicles": "A driver cannot be assigned to more than one selected vehicle."}
            )
        for selection in attrs["vehicles"]:
            if selection.get("driver_id"):
                driver = Driver.objects.filter(
                    organization=organization,
                    pk=selection["driver_id"],
                    depot=attrs["depot"],
                    status=Driver.Status.AVAILABLE,
                ).first()
                if not driver:
                    raise serializers.ValidationError({"vehicles": "A selected driver is unavailable or out of depot scope."})
        return attrs

    def create(self, validated_data):
        order_ids = validated_data.pop("order_ids")
        vehicles = validated_data.pop("vehicles")
        request = self.context["request"]
        run = PlanningRun.objects.create(
            organization=request.organization,
            requested_by=request.user,
            **validated_data,
        )
        PlanningRunOrder.objects.bulk_create(
            [PlanningRunOrder(planning_run=run, order=order) for order in order_ids]
        )
        for selection in vehicles:
            vehicle = Vehicle.objects.get(pk=selection["vehicle_id"], organization=request.organization)
            driver = None
            if selection.get("driver_id"):
                driver = Driver.objects.get(pk=selection["driver_id"], organization=request.organization)
            PlanningRunVehicle.objects.create(
                planning_run=run,
                vehicle=vehicle,
                driver=driver,
                locked=bool(selection.get("locked", False)),
                availability_snapshot_json={"vehicle_version": vehicle.version},
            )
        return run


class PlanningRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanningRun
        fields = "__all__"


class RouteStopSerializer(serializers.ModelSerializer):
    location = CoordinateField()
    order_reference = serializers.CharField(source="order.external_ref", read_only=True)
    address_text = serializers.CharField(source="address.one_line", read_only=True)

    class Meta:
        model = RouteStop
        fields = "__all__"
        read_only_fields = ["route", "version"]


class RouteSerializer(serializers.ModelSerializer):
    stops = RouteStopSerializer(many=True, read_only=True)
    vehicle_code = serializers.CharField(source="vehicle.code", read_only=True)
    driver_name = serializers.CharField(source="driver.full_name", read_only=True)
    geometry = serializers.SerializerMethodField()

    class Meta:
        model = Route
        fields = "__all__"

    @extend_schema_field({"type": "object", "nullable": True})
    def get_geometry(self, obj):
        if not obj.geometry:
            return None
        return {"type": "LineString", "coordinates": [list(point) for point in obj.geometry.coords]}


class ViolationSerializer(serializers.ModelSerializer):
    class Meta:
        model = RouteViolation
        fields = "__all__"


class UnassignedOrderSerializer(serializers.ModelSerializer):
    order_reference = serializers.CharField(source="order.external_ref", read_only=True)

    class Meta:
        model = UnassignedOrder
        fields = "__all__"


class RoutePlanSerializer(serializers.ModelSerializer):
    routes = RouteSerializer(many=True, read_only=True)
    violations = ViolationSerializer(many=True, read_only=True)
    unassigned_orders = UnassignedOrderSerializer(many=True, read_only=True)

    class Meta:
        model = RoutePlan
        fields = "__all__"


class OperationExceptionSerializer(OrganizationScopedSerializer):
    class Meta:
        model = OperationException
        exclude = ["organization"]
        read_only_fields = ["reported_by", "resolved_at", "version"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        organization = self.organization
        order = _instance_value(self, attrs, "order")
        route = _instance_value(self, attrs, "route")
        route_stop = _instance_value(self, attrs, "route_stop")
        assigned_to = _instance_value(self, attrs, "assigned_to")
        errors = {}
        if order and order.organization_id != organization.pk:
            errors["order"] = "Must belong to the active organization."
        if route and route.route_plan.organization_id != organization.pk:
            errors["route"] = "Must belong to the active organization."
        if route_stop and route_stop.route.route_plan.organization_id != organization.pk:
            errors["route_stop"] = "Must belong to the active organization."
        if route and route_stop and route_stop.route_id != route.pk:
            errors["route_stop"] = "Must belong to the selected route."
        if assigned_to and not assigned_to.organization_memberships.filter(
            organization=organization,
            active=True,
        ).exists():
            errors["assigned_to"] = "Must have an active membership in the organization."
        if errors:
            raise serializers.ValidationError(errors)
        scoped_depots = {}
        if order:
            scoped_depots["order"] = order.depot
        if route:
            scoped_depots["route"] = route.route_plan.depot
        if route_stop:
            scoped_depots["route_stop"] = route_stop.route.route_plan.depot
        _validate_depot_access(self.context["request"], **scoped_depots)
        return attrs


class DriverEventSerializer(serializers.ModelSerializer):
    location = CoordinateField(required=False, allow_null=True)

    class Meta:
        model = DriverEvent
        fields = "__all__"


class VehicleLocationSerializer(serializers.ModelSerializer):
    location = CoordinateField()

    class Meta:
        model = VehicleLocation
        fields = "__all__"


class ProofOfDeliverySerializer(serializers.ModelSerializer):
    captured_location = CoordinateField(required=False, allow_null=True)

    class Meta:
        model = ProofOfDelivery
        fields = "__all__"
        read_only_fields = ["verified_by", "verified_at"]


class ReportExportSerializer(OrganizationScopedSerializer):
    class Meta:
        model = ReportExport
        exclude = ["organization"]
        read_only_fields = ["status", "output_file", "requested_by", "completed_at", "error_message"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        filters = attrs.get("filters_json", {})
        if not isinstance(filters, dict):
            raise serializers.ValidationError({"filters_json": "Must be an object."})
        parsed_dates = {}
        for field_name in ("date_from", "date_to"):
            if filters.get(field_name):
                parsed_dates[field_name] = serializers.DateField().run_validation(
                    filters[field_name]
                )
                filters[field_name] = parsed_dates[field_name].isoformat()
        if (
            parsed_dates.get("date_from")
            and parsed_dates.get("date_to")
            and parsed_dates["date_to"] < parsed_dates["date_from"]
        ):
            raise serializers.ValidationError(
                {"filters_json": "date_to must be on or after date_from."}
            )
        allowed_ids = membership_depot_ids(self.context["request"])
        depot_id = filters.get("depot_id")
        if depot_id is not None:
            try:
                depot_id = int(depot_id)
            except (TypeError, ValueError) as exc:
                raise serializers.ValidationError(
                    {"filters_json": "depot_id must be an integer."}
                ) from exc
            if not Depot.objects.filter(pk=depot_id, organization=self.organization).exists():
                raise serializers.ValidationError(
                    {"filters_json": "depot_id must belong to the active organization."}
                )
            filters["depot_id"] = depot_id
        if allowed_ids is not None:
            if depot_id is None:
                raise serializers.ValidationError(
                    {"filters_json": "depot_id is required for a depot-scoped export."}
                )
            if depot_id not in allowed_ids:
                raise serializers.ValidationError(
                    {"filters_json": "depot_id is outside your authorized depot scope."}
                )
        return attrs


class WebhookSubscriptionSerializer(OrganizationScopedSerializer):
    class Meta:
        model = WebhookSubscription
        exclude = ["organization"]

    def validate_url(self, value):
        # Refuses non-HTTPS and any host that resolves to a private, loopback or
        # link-local address. Without this the delivery task can be aimed at the
        # deployment's own network and the response read back out of
        # WebhookDelivery.response_body.
        return validate_webhook_url(value)

    def validate_event_types(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of event names.")
        unknown = [name for name in value if name != "*" and name not in EVENT_TYPES]
        if unknown:
            raise serializers.ValidationError(
                f"Unknown event type(s): {', '.join(sorted(unknown))}. "
                f"Supported: {', '.join(EVENT_TYPES)} or '*' for all."
            )
        if not value:
            raise serializers.ValidationError("Subscribe to at least one event, or '*' for all.")
        return value


class DriverTokenSerializer(TokenObtainPairSerializer):
    device_id = serializers.UUIDField(required=False)
    device_name = serializers.CharField(required=False, default="Driver browser")

    def validate(self, attrs):
        device_id = attrs.pop("device_id", None) or uuid.uuid4()
        device_name = attrs.pop("device_name", "Driver browser")
        data = super().validate(attrs)
        if not hasattr(self.user, "driver_profile"):
            raise serializers.ValidationError("This account is not associated with a driver.")
        driver = self.user.driver_profile
        device, _ = DriverDevice.objects.update_or_create(
            driver=driver,
            device_id=device_id,
            defaults={"name": device_name, "revoked_at": None, "last_seen_at": timezone.now()},
        )
        refresh = RefreshToken(data["refresh"])
        refresh["driver_id"] = driver.pk
        refresh["organization_id"] = driver.organization_id
        refresh["device_id"] = str(device.device_id)
        access = refresh.access_token
        device.refresh_jti = refresh["jti"]
        device.save(update_fields=["refresh_jti", "updated_at"])
        data.update(
            {
                "refresh": str(refresh),
                "access": str(access),
                "device_id": str(device.device_id),
                "driver_id": driver.pk,
            }
        )
        return data
