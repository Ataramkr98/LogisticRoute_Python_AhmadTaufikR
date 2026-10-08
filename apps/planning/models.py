from django.conf import settings
from django.contrib.gis.db import models

from apps.common.models import TimeStampedModel, VersionedModel


class PlanningProfile(TimeStampedModel, VersionedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="planning_profiles")
    name = models.CharField(max_length=180)
    objective_weights_json = models.JSONField(default=dict)
    default_constraints_json = models.JSONField(default=dict)
    routing_profile = models.CharField(max_length=40, default="driving")
    traffic_mode = models.CharField(max_length=40, default="none")
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "name", "version"], name="uq_planning_profile_version")]

    def __str__(self):
        return self.name


class PlanningRun(TimeStampedModel, VersionedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        MATRIX_PENDING = "MATRIX_PENDING", "Matrix pending"
        OPTIMIZING = "OPTIMIZING", "Optimizing"
        OPTIMIZED = "OPTIMIZED", "Optimized"
        REVIEW = "REVIEW", "Review"
        DISPATCHED = "DISPATCHED", "Dispatched"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"
        SUPERSEDED = "SUPERSEDED", "Superseded"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="planning_runs")
    depot = models.ForeignKey("depots.Depot", on_delete=models.PROTECT, related_name="planning_runs")
    service_date = models.DateField()
    profile = models.ForeignKey(PlanningProfile, on_delete=models.PROTECT, related_name="planning_runs")
    status = models.CharField(max_length=24, choices=Status, default=Status.DRAFT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="planning_runs")
    input_hash = models.CharField(max_length=64, blank=True)
    input_snapshot_json = models.JSONField(default=dict, blank=True)
    solver_version = models.CharField(max_length=40, blank=True)
    matrix_provider = models.CharField(max_length=40, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    current_phase = models.CharField(max_length=60, blank=True)
    progress_message = models.CharField(max_length=255, blank=True)
    cancel_requested = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    objective_score = models.BigIntegerField(null=True, blank=True)
    total_distance_m = models.PositiveBigIntegerField(default=0)
    total_duration_s = models.PositiveBigIntegerField(default=0)
    vehicles_used = models.PositiveIntegerField(default=0)
    unassigned_count = models.PositiveIntegerField(default=0)
    metrics_json = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=60, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        permissions = [("optimize_plan", "Can optimize route plan")]

    def __str__(self):
        return f"Run {self.pk} · {self.service_date}"


class PlanningRunOrder(models.Model):
    planning_run = models.ForeignKey(PlanningRun, on_delete=models.CASCADE, related_name="run_orders")
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="planning_entries")
    locked = models.BooleanField(default=False)
    eligibility_status = models.CharField(max_length=24, default="ELIGIBLE")
    exclusion_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["planning_run", "order"], name="uq_run_order")]


class PlanningRunVehicle(models.Model):
    planning_run = models.ForeignKey(PlanningRun, on_delete=models.CASCADE, related_name="run_vehicles")
    vehicle = models.ForeignKey("fleet.Vehicle", on_delete=models.PROTECT, related_name="planning_entries")
    driver = models.ForeignKey("drivers.Driver", on_delete=models.PROTECT, null=True, blank=True, related_name="planning_entries")
    locked = models.BooleanField(default=False)
    availability_snapshot_json = models.JSONField(default=dict)
    familiarity_score_json = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["planning_run", "vehicle"], name="uq_run_vehicle")]


class DistanceMatrix(TimeStampedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="distance_matrices")
    matrix_key = models.CharField(max_length=64)
    provider = models.CharField(max_length=40)
    profile = models.CharField(max_length=40)
    traffic_timestamp = models.DateTimeField(null=True, blank=True)
    location_count = models.PositiveIntegerField()
    matrix_storage_key = models.CharField(max_length=500)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "matrix_key"], name="uq_distance_matrix")]


class RoutePlan(TimeStampedModel, VersionedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        OPTIMIZED = "OPTIMIZED", "Optimized"
        REVIEW = "REVIEW", "Review"
        DISPATCHED = "DISPATCHED", "Dispatched"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        SUPERSEDED = "SUPERSEDED", "Superseded"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="route_plans")
    planning_run = models.ForeignKey(PlanningRun, on_delete=models.PROTECT, related_name="route_plans")
    service_date = models.DateField()
    depot = models.ForeignKey("depots.Depot", on_delete=models.PROTECT, related_name="route_plans")
    plan_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status, default=Status.DRAFT)
    supersedes = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="superseded_by")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="route_plans")
    dispatched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["planning_run", "plan_version"], name="uq_route_plan_version")]
        permissions = [
            ("create_plan", "Can create route plan"),
            ("dispatch_plan", "Can dispatch route plan"),
            ("override_plan", "Can override route plan"),
        ]


class Route(TimeStampedModel, VersionedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        DISPATCHED = "DISPATCHED", "Dispatched"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    route_plan = models.ForeignKey(RoutePlan, on_delete=models.CASCADE, related_name="routes")
    vehicle = models.ForeignKey("fleet.Vehicle", on_delete=models.PROTECT, related_name="routes")
    driver = models.ForeignKey("drivers.Driver", on_delete=models.PROTECT, null=True, blank=True, related_name="routes")
    sequence = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status, default=Status.DRAFT)
    start_at_planned = models.DateTimeField()
    end_at_planned = models.DateTimeField()
    actual_start_at = models.DateTimeField(null=True, blank=True)
    actual_end_at = models.DateTimeField(null=True, blank=True)
    total_distance_m = models.PositiveBigIntegerField(default=0)
    total_duration_s = models.PositiveBigIntegerField(default=0)
    total_service_s = models.PositiveBigIntegerField(default=0)
    total_waiting_s = models.PositiveBigIntegerField(default=0)
    total_load_weight_kg = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    total_load_volume_m3 = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    geometry = models.LineStringField(srid=4326, null=True, blank=True, spatial_index=False)
    encoded_polyline = models.TextField(blank=True)
    provider_route_id = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [models.Index(fields=["route_plan", "status"])]
        ordering = ["sequence"]


class RouteStop(TimeStampedModel, VersionedModel):
    class StopType(models.TextChoices):
        DEPOT_START = "DEPOT_START", "Depot start"
        PICKUP = "PICKUP", "Pickup"
        DELIVERY = "DELIVERY", "Delivery"
        BREAK = "BREAK", "Break"
        DEPOT_END = "DEPOT_END", "Depot end"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        EN_ROUTE = "EN_ROUTE", "En route"
        ARRIVED = "ARRIVED", "Arrived"
        SERVICING = "SERVICING", "Servicing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"
        CANCELLED = "CANCELLED", "Cancelled"

    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="stops")
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, null=True, blank=True, related_name="route_stops")
    stop_type = models.CharField(max_length=20, choices=StopType)
    sequence = models.PositiveIntegerField()
    address = models.ForeignKey("customers.Address", on_delete=models.PROTECT, null=True, blank=True, related_name="route_stops")
    location = models.PointField(srid=4326)
    planned_arrival_at = models.DateTimeField()
    planned_departure_at = models.DateTimeField()
    actual_arrival_at = models.DateTimeField(null=True, blank=True)
    actual_departure_at = models.DateTimeField(null=True, blank=True)
    service_duration_s = models.PositiveIntegerField(default=0)
    load_weight_after_kg = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    load_volume_after_m3 = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    locked = models.BooleanField(default=False)
    violation_flags_json = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["route", "sequence"], name="uq_route_stop_sequence")]
        indexes = [models.Index(fields=["order"])]
        ordering = ["sequence"]


class UnassignedOrder(TimeStampedModel):
    route_plan = models.ForeignKey(RoutePlan, on_delete=models.CASCADE, related_name="unassigned_orders")
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="unassigned_results")
    reason_code = models.CharField(max_length=60)
    explanation = models.TextField()
    violated_constraints_json = models.JSONField(default=list)


class RouteViolation(TimeStampedModel):
    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Warning"
        ERROR = "ERROR", "Error"

    route_plan = models.ForeignKey(RoutePlan, on_delete=models.CASCADE, related_name="violations")
    route = models.ForeignKey(Route, on_delete=models.CASCADE, null=True, blank=True, related_name="violations")
    route_stop = models.ForeignKey(RouteStop, on_delete=models.CASCADE, null=True, blank=True, related_name="violations")
    severity = models.CharField(max_length=12, choices=Severity)
    constraint_type = models.CharField(max_length=60)
    message = models.TextField()
    details_json = models.JSONField(default=dict)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="acknowledged_violations")
    acknowledged_at = models.DateTimeField(null=True, blank=True)

