import csv
import io
from decimal import Decimal, InvalidOperation

from celery import shared_task
from django.db import transaction
from django.utils.dateparse import parse_date, parse_datetime

from apps.customers.models import Address, Customer
from apps.orders.models import ImportJob, ImportRowError, Order
from apps.routing.services import geocode_address

REQUIRED_COLUMNS = {"external_ref", "service_date", "customer_name", "address_line1", "city", "country_code"}


def _decimal(value, default="0"):
    try:
        return Decimal(value or default)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value: {value}") from exc


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, max_retries=3)
def geocode_order_task(self, order_id):
    order = Order.objects.select_related("pickup_address", "delivery_address").get(pk=order_id)
    addresses = [order.delivery_address]
    if order.pickup_address_id:
        addresses.insert(0, order.pickup_address)
    for address in addresses:
        if not address.location:
            geocode_address(address)
    needs_pickup = order.order_type == Order.Type.PICKUP_DELIVERY
    ready = bool(order.delivery_address.location) and (
        not needs_pickup or bool(order.pickup_address and order.pickup_address.location)
    )
    if ready and order.status == Order.Status.DRAFT:
        order.status = Order.Status.READY
        order.version += 1
        order.save(update_fields=["status", "version", "updated_at"])
    return order.pk


@shared_task(bind=True)
def import_orders_task(self, import_job_id):
    job = ImportJob.objects.select_related("organization", "depot", "requested_by").get(pk=import_job_id)
    job.status = ImportJob.Status.PROCESSING
    job.save(update_fields=["status", "updated_at"])
    try:
        with job.source_file.open("rb") as source:
            text = io.TextIOWrapper(source, encoding="utf-8-sig")
            reader = csv.DictReader(text)
            missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
            rows = list(reader)
        job.total_rows = len(rows)
        job.save(update_fields=["total_rows", "updated_at"])
        for row_number, row in enumerate(rows, start=2):
            try:
                with transaction.atomic():
                    service_date = parse_date(row["service_date"])
                    if not service_date:
                        raise ValueError("service_date must use YYYY-MM-DD")
                    customer_ref = row.get("customer_external_ref") or f"CSV-{row['customer_name'].strip().casefold()}"
                    customer, _ = Customer.objects.get_or_create(
                        organization=job.organization,
                        external_ref=customer_ref[:100],
                        defaults={"name": row["customer_name"].strip()},
                    )
                    address = Address.objects.create(
                        organization=job.organization,
                        customer=customer,
                        label="Imported delivery",
                        line1=row["address_line1"].strip(),
                        line2=row.get("address_line2", "").strip(),
                        city=row["city"].strip(),
                        region=row.get("region", "").strip(),
                        postal_code=row.get("postal_code", "").strip(),
                        country_code=row["country_code"].strip().upper(),
                    )
                    geocode_address(address)
                    order, created = Order.objects.get_or_create(
                        organization=job.organization,
                        external_ref=row["external_ref"].strip(),
                        defaults={
                            "depot": job.depot,
                            "customer": customer,
                            "order_type": row.get("order_type") or Order.Type.DELIVERY,
                            "status": Order.Status.READY,
                            "priority": int(row.get("priority") or 3),
                            "delivery_address": address,
                            "service_date": service_date,
                            "time_window_start": parse_datetime(row.get("time_window_start", "")),
                            "time_window_end": parse_datetime(row.get("time_window_end", "")),
                            "service_duration_seconds": int(row.get("service_duration_seconds") or 300),
                            "demand_weight_kg": _decimal(row.get("demand_weight_kg")),
                            "demand_volume_m3": _decimal(row.get("demand_volume_m3")),
                            "package_count": int(row.get("package_count") or 1),
                            "required_skills_json": [
                                value.strip() for value in row.get("required_skills", "").split("|") if value.strip()
                            ],
                            "special_instructions": row.get("special_instructions", ""),
                            "created_by": job.requested_by,
                        },
                    )
                    if not created:
                        address.delete()
                    job.accepted_rows += 1
            except Exception as exc:
                ImportRowError.objects.create(
                    import_job=job,
                    row_number=row_number,
                    code="ROW_INVALID",
                    message=str(exc),
                    raw_data=row,
                )
                job.rejected_rows += 1
            job.processed_rows += 1
            if job.processed_rows % 25 == 0:
                job.save(
                    update_fields=[
                        "processed_rows",
                        "accepted_rows",
                        "rejected_rows",
                        "updated_at",
                    ]
                )
        job.status = ImportJob.Status.COMPLETED
        job.save(
            update_fields=[
                "status",
                "processed_rows",
                "accepted_rows",
                "rejected_rows",
                "updated_at",
            ]
        )
        return job.pk
    except Exception as exc:
        job.status = ImportJob.Status.FAILED
        job.error_message = str(exc)
        job.save(update_fields=["status", "error_message", "updated_at"])
        raise
