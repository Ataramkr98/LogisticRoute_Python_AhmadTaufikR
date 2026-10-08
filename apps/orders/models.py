from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel, VersionedModel


class Order(TimeStampedModel, VersionedModel):
    class Type(models.TextChoices):
        DELIVERY = "DELIVERY", "Delivery"
        PICKUP = "PICKUP", "Pickup"
        PICKUP_DELIVERY = "PICKUP_DELIVERY", "Pickup and delivery"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        READY = "READY", "Ready"
        PLANNED = "PLANNED", "Planned"
        DISPATCHED = "DISPATCHED", "Dispatched"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"
        PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED", "Partially completed"
        UNASSIGNED = "UNASSIGNED", "Unassigned"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="orders")
    external_ref = models.CharField(max_length=100)
    depot = models.ForeignKey("depots.Depot", on_delete=models.PROTECT, related_name="orders")
    customer = models.ForeignKey("customers.Customer", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    order_type = models.CharField(max_length=24, choices=Type, default=Type.DELIVERY)
    status = models.CharField(max_length=24, choices=Status, default=Status.DRAFT)
    priority = models.PositiveSmallIntegerField(default=3)
    pickup_address = models.ForeignKey("customers.Address", on_delete=models.PROTECT, null=True, blank=True, related_name="pickup_orders")
    delivery_address = models.ForeignKey("customers.Address", on_delete=models.PROTECT, related_name="delivery_orders")
    service_date = models.DateField()
    time_window_start = models.DateTimeField(null=True, blank=True)
    time_window_end = models.DateTimeField(null=True, blank=True)
    service_duration_seconds = models.PositiveIntegerField(default=300)
    demand_weight_kg = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    demand_volume_m3 = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    package_count = models.PositiveIntegerField(default=1)
    required_skills_json = models.JSONField(default=list, blank=True)
    special_instructions = models.TextField(blank=True)
    preferred_driver = models.ForeignKey("drivers.Driver", on_delete=models.SET_NULL, null=True, blank=True, related_name="preferred_orders")
    assigned_route_stop = models.OneToOneField("planning.RouteStop", on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_order")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_orders")
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "external_ref"], name="uq_order_ref")]
        indexes = [
            models.Index(fields=["organization", "service_date", "status"]),
            models.Index(fields=["depot", "service_date", "status"]),
        ]
        permissions = [("cancel_order", "Can cancel order")]

    def __str__(self):
        return self.external_ref


class OrderItem(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    sku = models.CharField(max_length=100)
    name = models.CharField(max_length=180)
    quantity = models.PositiveIntegerField(default=1)
    weight_kg = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    volume_m3 = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    metadata_json = models.JSONField(default=dict, blank=True)


class OrderTimeWindow(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="time_windows")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    preference_weight = models.PositiveIntegerField(default=1)
    hard_constraint = models.BooleanField(default=True)


class ImportJob(TimeStampedModel):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="import_jobs")
    depot = models.ForeignKey("depots.Depot", on_delete=models.PROTECT, related_name="import_jobs")
    source_file = models.FileField(upload_to="imports/%Y/%m/")
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    accepted_rows = models.PositiveIntegerField(default=0)
    rejected_rows = models.PositiveIntegerField(default=0)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="order_imports")
    error_message = models.TextField(blank=True)


class ImportRowError(models.Model):
    import_job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name="row_errors")
    row_number = models.PositiveIntegerField()
    code = models.CharField(max_length=60)
    message = models.TextField()
    raw_data = models.JSONField(default=dict)

    class Meta:
        ordering = ["row_number"]

