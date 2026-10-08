from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.audit.models import IdempotencyRecord

from .models import VehicleLocation


@shared_task
def prune_location_history():
    cutoff = timezone.now() - timezone.timedelta(days=settings.GPS_RETENTION_DAYS)
    deleted, _ = VehicleLocation.objects.filter(recorded_at__lt=cutoff).delete()
    return deleted


@shared_task
def prune_idempotency_records():
    cutoff = timezone.now() - timezone.timedelta(days=30)
    deleted, _ = IdempotencyRecord.objects.filter(updated_at__lt=cutoff).delete()
    return deleted

