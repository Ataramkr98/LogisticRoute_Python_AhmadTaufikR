import uuid

from django.conf import settings
from django.contrib.gis.db import models

from apps.common.fields import EncryptedTextField
from apps.common.models import TimeStampedModel, VersionedModel


class Driver(TimeStampedModel, VersionedModel):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        ON_ROUTE = "ON_ROUTE", "On route"
        OFF_DUTY = "OFF_DUTY", "Off duty"
        INACTIVE = "INACTIVE", "Inactive"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="drivers")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="driver_profile")
    depot = models.ForeignKey("depots.Depot", on_delete=models.PROTECT, related_name="drivers")
    employee_code = models.CharField(max_length=40)
    full_name = models.CharField(max_length=180)
    phone_encrypted = EncryptedTextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=Status, default=Status.AVAILABLE)
    skills_json = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "employee_code"], name="uq_driver_code")]

    def __str__(self):
        return self.full_name


class DriverShift(TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="shifts")
    shift_date = models.DateField()
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    start_location = models.PointField(srid=4326, null=True, blank=True, spatial_index=False)
    end_location = models.PointField(srid=4326, null=True, blank=True, spatial_index=False)
    break_rules_json = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.SCHEDULED)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["driver", "shift_date", "start_at"], name="uq_driver_shift")]


class DriverDevice(TimeStampedModel):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="devices")
    device_id = models.UUIDField(default=uuid.uuid4)
    name = models.CharField(max_length=120)
    refresh_jti = models.CharField(max_length=255, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["driver", "device_id"], name="uq_driver_device")]

