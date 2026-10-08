import uuid

from apps.common.dispatch import dispatch

from .models import WebhookDelivery, WebhookSubscription
from .tasks import deliver_webhook_task

#: Event names emitted by the domain. Kept as an explicit list so the set of
#: notifications is reviewable, and so a subscription can be validated against it
#: instead of accepting any string.
EVENT_TYPES = (
    "order.created",
    "order.cancelled",
    "plan.optimized",
    "plan.dispatched",
    "route.completed",
    "exception.raised",
    "exception.resolved",
    "pod.captured",
)


def emit_webhook_event(organization, event_type, payload):
    """Queue a signed delivery for every subscription listening to ``event_type``.

    Called from the service layer rather than from views, so the notification
    fires for API, web and background paths alike. It was previously defined but
    never called, which left the whole delivery pipeline — signing, retry and the
    integrations status board — inert while the UI advertised it as working.
    """
    deliveries = []
    subscriptions = WebhookSubscription.objects.filter(organization=organization, active=True)
    for subscription in subscriptions:
        wanted = subscription.event_types or []
        if "*" not in wanted and event_type not in wanted:
            continue
        delivery = WebhookDelivery.objects.create(
            subscription=subscription,
            event_type=event_type,
            event_id=str(uuid.uuid4()),
            payload_json=payload,
        )
        dispatch(deliver_webhook_task, delivery.pk)
        deliveries.append(delivery)
    return deliveries


