from django.db import models

from apps.common.fields import EncryptedTextField
from apps.common.models import TimeStampedModel


class Notification(TimeStampedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="notifications")
    recipient = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="notifications")
    type = models.CharField(max_length=60)
    title = models.CharField(max_length=180)
    message = models.TextField()
    payload_json = models.JSONField(default=dict)
    read_at = models.DateTimeField(null=True, blank=True)


class WebhookSubscription(TimeStampedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="webhooks")
    name = models.CharField(max_length=180)
    url = models.URLField(max_length=500)
    secret_encrypted = EncryptedTextField()
    event_types = models.JSONField(default=list)
    active = models.BooleanField(default=True)


class WebhookDelivery(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DELIVERED = "DELIVERED", "Delivered"
        RETRYING = "RETRYING", "Retrying"
        FAILED = "FAILED", "Failed"

    subscription = models.ForeignKey(WebhookSubscription, on_delete=models.CASCADE, related_name="deliveries")
    event_type = models.CharField(max_length=60)
    event_id = models.CharField(max_length=100)
    payload_json = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["subscription", "event_id"], name="uq_webhook_event")]

