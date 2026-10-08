import csv
import io

from celery import shared_task
from django.core.files.base import ContentFile
from django.db.models import Count, Sum
from django.utils import timezone

from apps.audit.services import record_audit
from apps.orders.models import Order
from apps.planning.models import Route

from .models import ReportExport


@shared_task(bind=True)
def export_report_task(self, export_id):
    export = ReportExport.objects.select_related("organization", "requested_by").get(pk=export_id)
    export.status = ReportExport.Status.PROCESSING
    export.save(update_fields=["status", "updated_at"])
    try:
        filters = export.filters_json
        start = filters.get("date_from")
        end = filters.get("date_to")
        depot_id = filters.get("depot_id")
        orders = Order.objects.filter(organization=export.organization)
        routes = Route.objects.filter(route_plan__organization=export.organization)
        if depot_id:
            orders = orders.filter(depot_id=depot_id)
            routes = routes.filter(route_plan__depot_id=depot_id)
        if start:
            orders = orders.filter(service_date__gte=start)
            routes = routes.filter(route_plan__service_date__gte=start)
        if end:
            orders = orders.filter(service_date__lte=end)
            routes = routes.filter(route_plan__service_date__lte=end)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["metric", "value"])
        for row in orders.values("status").annotate(value=Count("id")).order_by("status"):
            writer.writerow([f"orders_{row['status'].lower()}", row["value"]])
        totals = routes.aggregate(distance_m=Sum("total_distance_m"), duration_s=Sum("total_duration_s"))
        writer.writerow(["total_planned_distance_m", totals["distance_m"] or 0])
        writer.writerow(["total_planned_duration_s", totals["duration_s"] or 0])
        writer.writerow(["routes", routes.count()])
        export.output_file.save(
            f"{export.report_type}-{export.pk}.csv",
            ContentFile(output.getvalue().encode("utf-8")),
            save=False,
        )
        export.status = ReportExport.Status.COMPLETED
        export.completed_at = timezone.now()
        export.save(update_fields=["output_file", "status", "completed_at", "updated_at"])
        record_audit(
            organization=export.organization,
            actor=export.requested_by,
            action="report.exported",
            instance=export,
            after={"report_type": export.report_type},
        )
        return export.pk
    except Exception as exc:
        export.status = ReportExport.Status.FAILED
        export.error_message = str(exc)
        export.save(update_fields=["status", "error_message", "updated_at"])
        raise
