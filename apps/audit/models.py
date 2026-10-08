from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel


class AuditLog(models.Model):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    action = models.CharField(max_length=100)
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=100)
    before_json = models.JSONField(default=dict, blank=True)
    after_json = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "resource_type", "resource_id"])]
        permissions = [("view_audit", "Can view audit log")]


class IdempotencyRecord(TimeStampedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="idempotency_records")
    actor_key = models.CharField(max_length=100)
    operation = models.CharField(max_length=100)
    idempotency_key = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_json = models.JSONField(default=dict, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "actor_key", "operation", "idempotency_key"],
                name="uq_idempotency_scope",
            )
        ]

