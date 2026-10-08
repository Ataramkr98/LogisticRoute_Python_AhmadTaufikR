import hashlib
import hmac
import json
import time

import httpx
from celery import shared_task
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import WebhookDelivery
from .validation import validate_webhook_url


@shared_task(bind=True, max_retries=5)
def deliver_webhook_task(self, delivery_id):
    delivery = WebhookDelivery.objects.select_related("subscription").get(pk=delivery_id)
    body = json.dumps(delivery.payload_json, separators=(",", ":"), default=str).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        delivery.subscription.secret_encrypted.encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    delivery.attempt_count += 1
    try:
        # Re-validated immediately before sending. DNS can be re-pointed at a
        # private address after the subscription was saved, so the check at save
        # time is necessary but not sufficient.
        validate_webhook_url(delivery.subscription.url)
        response = httpx.post(
            delivery.subscription.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Logistics-Timestamp": timestamp,
                "X-Logistics-Signature": f"v1={signature}",
                "X-Logistics-Event": delivery.event_type,
            },
            timeout=15,
            follow_redirects=False,
        )
        delivery.response_status = response.status_code
        delivery.response_body = response.text[:2000]
        if 200 <= response.status_code < 300:
            delivery.status = WebhookDelivery.Status.DELIVERED
            delivery.next_attempt_at = None
            delivery.save()
            return delivery.pk
        response.raise_for_status()
    except ValidationError as exc:
        # An unreachable or private target is a permanent condition, so retrying
        # five times would only delay the same failure.
        delivery.status = WebhookDelivery.Status.FAILED
        delivery.response_body = str(exc)[:2000]
        delivery.next_attempt_at = None
        delivery.save()
        raise
    except httpx.HTTPError as exc:
        if self.request.retries >= self.max_retries:
            delivery.status = WebhookDelivery.Status.FAILED
            delivery.next_attempt_at = None
            delivery.save()
            raise
        delay = min(3600, 2 ** self.request.retries * 30)
        delivery.status = WebhookDelivery.Status.RETRYING
        delivery.next_attempt_at = timezone.now() + timezone.timedelta(seconds=delay)
        delivery.save()
        raise self.retry(exc=exc, countdown=delay) from exc
