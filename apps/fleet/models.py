from django.contrib.gis.db import models

from apps.common.models import TimeStampedModel, VersionedModel


class Vehicle(TimeStampedModel, VersionedModel):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        IN_USE = "IN_USE", "In use"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        INACTIVE = "INACTIVE", "Inactive"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="vehicles")
    depot = models.ForeignKey("depots.Depot", on_delete=models.PROTECT, related_name="vehicles")
    code = models.CharField(max_length=40)
    plate_number = models.CharField(max_length=40)
    vehicle_type = models.CharField(max_length=60)
    status = models.CharField(max_length=20, choices=Status, default=Status.AVAILABLE)
    capacity_weight_kg = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    capacity_volume_m3 = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    capacity_package_count = models.PositiveIntegerField(default=0)
    max_stops = models.PositiveIntegerField(default=100)
    max_route_duration_seconds = models.PositiveIntegerField(default=43200)
    skills_json = models.JSONField(default=list, blank=True)
    start_location = models.PointField(srid=4326)
    end_location = models.PointField(srid=4326)
    fixed_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cost_per_km = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    cost_per_hour = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uq_vehicle_code"),
            models.UniqueConstraint(fields=["organization", "plate_number"], name="uq_vehicle_plate"),
        ]

    def __str__(self):
        return f"{self.code} · {self.plate_number}"


class VehicleAvailability(TimeStampedModel):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="availability_periods")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status)
    reason = models.CharField(max_length=255, blank=True)

