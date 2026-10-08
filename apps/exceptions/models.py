from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel, VersionedModel


class OperationException(TimeStampedModel, VersionedModel):
    class Severity(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        RESOLVED = "RESOLVED", "Resolved"
        CANCELLED = "CANCELLED", "Cancelled"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="exceptions")
    order = models.ForeignKey("orders.Order", on_delete=models.SET_NULL, null=True, blank=True, related_name="exceptions")
    route = models.ForeignKey("planning.Route", on_delete=models.SET_NULL, null=True, blank=True, related_name="exceptions")
    route_stop = models.ForeignKey("planning.RouteStop", on_delete=models.SET_NULL, null=True, blank=True, related_name="exceptions")
    type = models.CharField(max_length=60)
    severity = models.CharField(max_length=12, choices=Severity, default=Severity.MEDIUM)
    status = models.CharField(max_length=16, choices=Status, default=Status.OPEN)
    description = models.TextField()
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reported_exceptions")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_exceptions")
    resolution = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["organization", "status", "severity", "created_at"])]

