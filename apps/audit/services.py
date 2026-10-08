import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AuditLog, IdempotencyRecord


def _jsonable(value):
    """Make an arbitrary value safe to store in an audit JSON column.

    Callers pass whatever they have to hand -- datetimes, Decimals, UUIDs, model
    instances. The audit columns are JSONFields, so the payload has to be
    JSON-serialisable before it reaches the database. Recursing here means every
    caller can record rich context without having to remember to cast it, and a
    datetime can no longer abort a business operation such as dispatching a plan.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def record_audit(
    *,
    organization,
    actor,
    action,
    instance,
    before=None,
    after=None,
    request=None,
    resource_type=None,
    resource_id=None,
):
    """Append an audit row for a change to ``instance``.

    ``resource_type`` and ``resource_id`` default to the instance's own
    metadata. Callers must pass them explicitly after a delete, because Django
    clears ``instance.pk`` once the row is gone and the log would otherwise
    record a deletion against ``resource_id="None"``.
    """
    return AuditLog.objects.create(
        organization=organization,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        resource_type=resource_type or instance._meta.label_lower,
        resource_id=str(resource_id if resource_id is not None else instance.pk),
        before_json=_jsonable(before or {}),
        after_json=_jsonable(after or {}),
        ip_address=request.META.get("REMOTE_ADDR") if request else None,
        request_id=getattr(request, "request_id", "") if request else "",
    )


@dataclass
class IdempotentResult:
    record: IdempotencyRecord
    replayed: bool


def begin_idempotent(*, organization, actor_key, operation, key, payload):
    key = str(key or "").strip()
    if not key or len(key) > 100:
        raise ValueError("Idempotency key must contain 1 to 100 characters")
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    lookup = {
        "organization": organization,
        "actor_key": actor_key,
        "operation": operation,
        "idempotency_key": key,
    }

    def resolve_existing(record):
        if record.request_hash != request_hash:
            raise ValueError("Idempotency key was already used with a different payload")
        if record.response_status is None and record.locked_until and record.locked_until > timezone.now():
            raise ValueError("A request with this idempotency key is still processing")
        if record.response_status is None:
            record.locked_until = timezone.now() + timezone.timedelta(minutes=5)
            record.save(update_fields=["locked_until", "updated_at"])
        return IdempotentResult(record, record.response_status is not None)

    try:
        with transaction.atomic():
            record = IdempotencyRecord.objects.select_for_update().filter(**lookup).first()
            if record:
                return resolve_existing(record)
            record = IdempotencyRecord.objects.create(
                **lookup,
                request_hash=request_hash,
                locked_until=timezone.now() + timezone.timedelta(minutes=5),
            )
            return IdempotentResult(record, False)
    except IntegrityError:
        with transaction.atomic():
            record = IdempotencyRecord.objects.select_for_update().get(**lookup)
            return resolve_existing(record)


def complete_idempotent(record, status, response):
    record.response_status = status
    record.response_json = response
    record.locked_until = None
    record.save(update_fields=["response_status", "response_json", "locked_until", "updated_at"])
