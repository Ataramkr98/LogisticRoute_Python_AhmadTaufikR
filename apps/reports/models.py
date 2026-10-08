from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel


class ReportExport(TimeStampedModel):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="report_exports")
    report_type = models.CharField(max_length=60)
    filters_json = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    output_file = models.FileField(upload_to="reports/%Y/%m/", null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="report_exports")
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

