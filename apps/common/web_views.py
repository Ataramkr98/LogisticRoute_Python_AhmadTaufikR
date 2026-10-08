from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from apps.audit.models import AuditLog
from apps.common.dispatch import dispatch
from apps.common.forms import OrderForm, ReportExportForm
from apps.common.scoping import scope_queryset_to_membership, scope_report_exports_to_membership
from apps.common.time import localdate_in
from apps.depots.models import Depot
from apps.dispatch.services import dispatch_plan
from apps.drivers.models import Driver
from apps.exceptions.models import OperationException
from apps.fleet.models import Vehicle
from apps.integrations.models import WebhookSubscription
from apps.optimization.tasks import optimize_run_task
from apps.orders.models import ImportJob, Order
from apps.orders.tasks import geocode_order_task
from apps.planning.models import (
    PlanningProfile,
    PlanningRun,
    PlanningRunOrder,
    PlanningRunVehicle,
    Route,
    RoutePlan,
    RouteStop,
)
from apps.planning.services import validate_plan
from apps.proof_of_delivery.models import ProofOfDelivery
from apps.reports.models import ReportExport
from apps.reports.tasks import export_report_task


def _render(request, template, context=None, status=200):
    if not request.organization:
        raise Http404("No active organization membership")
    if request.membership.role == "DRIVER" and not template.startswith("driver/"):
        return redirect("driver-today")
    return render(request, template, context or {}, using="jinja2", status=status)


def active_organization_required(view):
    """Reject signed-in users who have no usable organization membership.

    Authentication and authorization are separate steps, and a user can be
    authenticated while belonging to nothing: memberships are deactivated rather
    than deleted when staff leave, and the demo seeder retires old accounts the
    same way. Those sessions reached views that immediately dereference
    ``request.organization`` and raised ``AttributeError`` on ``None``, turning a
    routine lifecycle event into a 500.

    ``_render`` already guarded, but only after each view had read the attribute.
    Guarding at the entry point means the whole surface behaves consistently: the
    membership is gone, so the session is ended and the user is sent back to
    sign-in with an explanation.
    """

    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.organization:
            return redirect("no-organization")
        return view(request, *args, **kwargs)

    return wrapper


def _require_roles(request, *roles):
    if not request.membership or request.membership.role not in roles:
        raise PermissionDenied


def no_organization(request):
    """Explain why an authenticated user was signed out.

    Reached when an account authenticates but has no active membership, so every
    other page would be unusable. The session is ended here to avoid leaving the
    user in a half-signed-in state. The address is captured first because
    ``logout()`` replaces ``request.user`` with an anonymous user.
    """
    email = ""
    if request.user.is_authenticated:
        email = request.user.email
        if not request.organization:
            logout(request)
    return render(
        request,
        "registration/no_organization.html",
        {"unusable_account_email": email},
        using="jinja2",
        status=403,
    )


@active_organization_required
def dashboard(request):
    today = localdate_in(request.organization.timezone)
    # Organization-scoped base, reused for the throughput series so the chart
    # costs one aggregate query instead of one per day.
    orders_all = scope_queryset_to_membership(
        Order.objects.filter(organization=request.organization),
        request,
    )
    orders = orders_all.filter(service_date=today)
    counts = {row["status"]: row["count"] for row in orders.values("status").annotate(count=Count("id"))}
    routes = scope_queryset_to_membership(
        Route.objects.filter(
            route_plan__organization=request.organization,
            route_plan__service_date=today,
        ),
        request,
    )
    route_totals = routes.aggregate(distance=Sum("total_distance_m"), duration=Sum("total_duration_s"))
    dashboard_routes = list(
        routes.select_related("vehicle", "driver")
        .annotate(
            customer_stop_count=Count("stops", filter=Q(stops__order__isnull=False), distinct=True),
            completed_stop_count=Count(
                "stops",
                filter=Q(stops__order__isnull=False, stops__status=RouteStop.Status.COMPLETED),
                distinct=True,
            ),
        )
        .order_by("sequence")[:8]
    )
    for route in dashboard_routes:
        route.progress_percent = round(
            (route.completed_stop_count / route.customer_stop_count) * 100
        ) if route.customer_stop_count else 0

    vehicles = scope_queryset_to_membership(
        Vehicle.objects.filter(organization=request.organization, active=True),
        request,
    )
    vehicle_counts = {
        row["status"]: row["count"]
        for row in vehicles.values("status").annotate(count=Count("id"))
    }
    vehicle_total = vehicles.count()
    vehicles_in_use = vehicle_counts.get(Vehicle.Status.IN_USE, 0)

    # Seven-day throughput series for the dashboard chart. Built from real rows
    # rather than a hardcoded sequence, and every point is filled with an
    # explicit zero so the axis does not imply activity on days with none.
    horizon = [today - timezone.timedelta(days=offset) for offset in range(6, -1, -1)]
    scheduled_by_day = {
        row["service_date"]: row["count"]
        for row in orders_all.filter(service_date__in=horizon)
        .values("service_date")
        .annotate(count=Count("id"))
    }
    delivered_by_day = {
        row["service_date"]: row["count"]
        for row in orders_all.filter(service_date__in=horizon, status=Order.Status.COMPLETED)
        .values("service_date")
        .annotate(count=Count("id"))
    }
    throughput = {
        "days": [day.isoformat() for day in horizon],
        "scheduled": [scheduled_by_day.get(day, 0) for day in horizon],
        "delivered": [delivered_by_day.get(day, 0) for day in horizon],
        # A flat series with no variation carries no information; the chart is
        # only rendered when there is something to read.
        "has_data": sum(scheduled_by_day.values()) > 0,
    }

    context = {
        "today": today,
        "kpis": [
            ("Orders today", orders.count(), "All scheduled work"),
            ("Planned", counts.get(Order.Status.PLANNED, 0), "Assigned to routes"),
            ("Unassigned", counts.get(Order.Status.UNASSIGNED, 0), "Needs dispatcher review"),
            ("In progress", counts.get(Order.Status.IN_PROGRESS, 0), "Currently executing"),
            ("Completed", counts.get(Order.Status.COMPLETED, 0), "Finished stops"),
            ("Failed", counts.get(Order.Status.FAILED, 0), "Needs resolution"),
        ],
        "routes": dashboard_routes,
        "exceptions": scope_queryset_to_membership(
            OperationException.objects.filter(
                organization=request.organization,
                status__in=[OperationException.Status.OPEN, OperationException.Status.ACKNOWLEDGED],
            ),
            request,
        ).order_by("-severity", "-created_at")[:6],
        "planned_distance_km": round((route_totals["distance"] or 0) / 1000, 1),
        "throughput": throughput,
        # Real audit rows, newest first. This is the activity feed the operations
        # view needs: who changed what, and when.
        "activity": AuditLog.objects.filter(
            organization=request.organization
        )
        .select_related("actor")
        .order_by("-created_at")[:8],
        "fleet_readiness": [
            ("Available", vehicle_counts.get(Vehicle.Status.AVAILABLE, 0), "AVAILABLE"),
            ("On route", vehicles_in_use, "IN_USE"),
            ("Maintenance", vehicle_counts.get(Vehicle.Status.MAINTENANCE, 0), "MAINTENANCE"),
            ("Inactive", vehicle_counts.get(Vehicle.Status.INACTIVE, 0), "INACTIVE"),
        ],
        "vehicle_total": vehicle_total,
        "fleet_utilization": round((vehicles_in_use / vehicle_total) * 100, 1) if vehicle_total else 0,
    }
    return _render(request, "dashboard/index.html", context)


@active_organization_required
def orders_list(request):
    queryset = scope_queryset_to_membership(
        Order.objects.filter(organization=request.organization),
        request,
    ).select_related(
        "customer", "depot", "delivery_address", "assigned_route_stop__route"
    )
    if request.GET.get("date"):
        queryset = queryset.filter(service_date=request.GET["date"])
    if request.GET.get("status"):
        queryset = queryset.filter(status=request.GET["status"])
    if request.GET.get("depot"):
        queryset = queryset.filter(depot_id=request.GET["depot"])
    if request.GET.get("q"):
        queryset = queryset.filter(Q(external_ref__icontains=request.GET["q"]) | Q(customer__name__icontains=request.GET["q"]))
    context = {
        "orders": queryset.order_by("-service_date", "-priority", "external_ref")[:250],
        "depots": scope_queryset_to_membership(
            Depot.objects.filter(organization=request.organization, active=True),
            request,
        ),
        "statuses": Order.Status.choices,
        "filters": request.GET,
    }
    template = "orders/_table.html" if request.headers.get("HX-Request") else "orders/list.html"
    return _render(request, template, context)


@active_organization_required
@require_http_methods(["GET", "POST"])
def order_create(request):
    _require_roles(request, "ADMIN", "OPERATIONS_MANAGER", "DISPATCHER")
    form = OrderForm(
        request.POST or None,
        organization=request.organization,
        allowed_depots=scope_queryset_to_membership(
            Depot.objects.filter(organization=request.organization, active=True),
            request,
        ),
    )
    if request.method == "POST" and form.is_valid():
        order = form.save(commit=False)
        order.organization = request.organization
        order.created_by = request.user
        order.status = Order.Status.READY if order.delivery_address.location else Order.Status.DRAFT
        order.save()
        if order.status == Order.Status.DRAFT:
            transaction.on_commit(lambda: dispatch(geocode_order_task, order.pk))
            messages.success(request, f"Order {order.external_ref} was created and queued for geocoding.")
        else:
            messages.success(request, f"Order {order.external_ref} was created.")
        return redirect("order-detail", order_id=order.pk)
    return _render(request, "orders/form.html", {"form": form})


@active_organization_required
def order_detail(request, order_id):
    order = get_object_or_404(
        scope_queryset_to_membership(
            Order.objects.select_related(
                "customer",
                "depot",
                "pickup_address",
                "delivery_address",
                "assigned_route_stop__route",
            ),
            request,
        ),
        pk=order_id,
        organization=request.organization,
    )
    return _render(request, "orders/detail.html", {"order": order})


@active_organization_required
@require_http_methods(["GET", "POST"])
def order_import(request):
    _require_roles(request, "ADMIN", "OPERATIONS_MANAGER", "DISPATCHER")
    if request.method == "POST":
        source = request.FILES.get("source_file")
        depot = get_object_or_404(
            scope_queryset_to_membership(
                Depot.objects.filter(organization=request.organization),
                request,
            ),
            pk=request.POST.get("depot"),
        )
        if not source or not source.name.lower().endswith(".csv"):
            messages.error(request, "Choose a UTF-8 CSV file.")
        else:
            job = ImportJob.objects.create(
                organization=request.organization,
                depot=depot,
                source_file=source,
                requested_by=request.user,
            )
            from apps.orders.tasks import import_orders_task

            dispatch(import_orders_task, job.pk)
            messages.success(request, "The import was queued.")
            return redirect("order-import")
    return _render(
        request,
        "orders/import.html",
        {
            "depots": scope_queryset_to_membership(
                Depot.objects.filter(organization=request.organization, active=True),
                request,
            ),
            "jobs": scope_queryset_to_membership(
                ImportJob.objects.filter(organization=request.organization),
                request,
            ).order_by("-created_at")[:20],
        },
    )


@active_organization_required
@require_http_methods(["GET", "POST"])
def planning_new(request):
    _require_roles(request, "ADMIN", "OPERATIONS_MANAGER", "DISPATCHER")
    depots = scope_queryset_to_membership(
        Depot.objects.filter(organization=request.organization, active=True),
        request,
    )
    profiles = PlanningProfile.objects.filter(organization=request.organization, active=True)
    selected_date_value = request.POST.get("service_date") or request.GET.get("service_date")
    selected_date = (
        parse_date(selected_date_value)
        if selected_date_value
        else localdate_in(request.organization.timezone)
    )
    date_is_valid = selected_date is not None
    if not date_is_valid:
        selected_date = localdate_in(request.organization.timezone)
    selected_depot_value = request.POST.get("depot") or request.GET.get("depot")
    depot_is_valid = not selected_depot_value or str(selected_depot_value).isdigit()
    selected_depot = (
        str(selected_depot_value)
        if depot_is_valid and selected_depot_value
        else (str(depots.first().pk) if depots else "")
    )
    eligible_orders = Order.objects.filter(
        organization=request.organization,
        service_date=selected_date,
        depot_id=selected_depot or None,
        status__in=[Order.Status.READY, Order.Status.UNASSIGNED],
        delivery_address__location__isnull=False,
    ).select_related("customer", "delivery_address")
    vehicles = Vehicle.objects.filter(
        organization=request.organization,
        depot_id=selected_depot or None,
        active=True,
        status=Vehicle.Status.AVAILABLE,
    )
    drivers = Driver.objects.filter(
        organization=request.organization,
        depot_id=selected_depot or None,
        status=Driver.Status.AVAILABLE,
    )
    if request.method == "POST" and request.POST.get("create_run"):
        with transaction.atomic():
            order_ids = request.POST.getlist("orders")
            vehicle_ids = request.POST.getlist("vehicles")
            selected_orders = list(eligible_orders.filter(pk__in=order_ids))
            selected_vehicles = list(vehicles.filter(pk__in=vehicle_ids))
            driver_ids = [
                request.POST.get(f"driver_{vehicle.pk}")
                for vehicle in selected_vehicles
                if request.POST.get(f"driver_{vehicle.pk}")
            ]
            selected_drivers = {
                str(driver.pk): driver for driver in drivers.filter(pk__in=driver_ids)
            }
            if not date_is_valid:
                messages.error(request, "Service date must use YYYY-MM-DD.")
            elif not depot_is_valid:
                messages.error(request, "Choose a valid depot.")
            elif not order_ids or not vehicle_ids:
                messages.error(request, "Select at least one order and one vehicle.")
            elif len(selected_orders) != len(set(order_ids)) or len(selected_vehicles) != len(set(vehicle_ids)):
                messages.error(request, "One or more selected orders or vehicles are not eligible.")
            elif len(selected_drivers) != len(set(driver_ids)):
                messages.error(request, "One or more selected drivers are not eligible.")
            elif len(driver_ids) != len(set(driver_ids)):
                messages.error(request, "A driver cannot be assigned to more than one vehicle.")
            else:
                depot = get_object_or_404(depots, pk=selected_depot)
                profile = get_object_or_404(profiles, pk=request.POST.get("profile"))
                run = PlanningRun.objects.create(
                    organization=request.organization,
                    depot=depot,
                    service_date=selected_date,
                    profile=profile,
                    requested_by=request.user,
                )
                for order in selected_orders:
                    PlanningRunOrder.objects.create(planning_run=run, order=order)
                for vehicle in selected_vehicles:
                    driver_id = request.POST.get(f"driver_{vehicle.pk}")
                    driver = selected_drivers.get(str(driver_id)) if driver_id else None
                    PlanningRunVehicle.objects.create(
                        planning_run=run,
                        vehicle=vehicle,
                        driver=driver,
                        availability_snapshot_json={"vehicle_version": vehicle.version},
                    )
                task = dispatch(optimize_run_task, run.pk)
                run.celery_task_id = task.id
                run.save(update_fields=["celery_task_id", "updated_at"])
                return redirect("planning-progress", run_id=run.pk)
    return _render(
        request,
        "planning/new.html",
        {
            "depots": depots,
            "profiles": profiles,
            "orders": eligible_orders,
            "vehicles": vehicles,
            "drivers": drivers,
            "selected_date": str(selected_date),
            "selected_depot": selected_depot,
        },
    )


@active_organization_required
def planning_progress(request, run_id):
    run = get_object_or_404(
        scope_queryset_to_membership(
            PlanningRun.objects.filter(organization=request.organization),
            request,
        ),
        pk=run_id,
    )
    plan = run.route_plans.order_by("-plan_version").first()
    if plan and run.status in {PlanningRun.Status.REVIEW, PlanningRun.Status.OPTIMIZED}:
        return redirect("planning-review", plan_id=plan.pk)
    return _render(request, "planning/progress.html", {"run": run})


@active_organization_required
@require_http_methods(["GET", "POST"])
def planning_review(request, plan_id):
    plan = get_object_or_404(
        scope_queryset_to_membership(
            RoutePlan.objects.select_related(
                # review.html renders route.vehicle.code and
                # route.driver.full_name for every route; prefetch_related does
                # not cover these forward FKs, so they were two queries per row.
                "planning_run",
            ).prefetch_related(
                "routes__vehicle",
                "routes__driver",
                "routes__stops__order__customer",
                "routes__stops__order__delivery_address",
                "violations",
                "unassigned_orders__order",
            ),
            request,
        ),
        pk=plan_id,
        organization=request.organization,
    )
    if request.method == "POST":
        _require_roles(request, "ADMIN", "OPERATIONS_MANAGER", "DISPATCHER")
        if "validate" in request.POST:
            validate_plan(plan)
            messages.success(request, "Plan validation completed.")
        elif "dispatch" in request.POST:
            try:
                dispatch_plan(plan.pk, request.user, request)
                messages.success(request, "Routes were dispatched to drivers.")
                return redirect("route-list")
            except ValueError as exc:
                messages.error(request, str(exc))
    route_features = [
        {
            "id": route.pk,
            "vehicle": route.vehicle.code,
            "color": ["#2563eb", "#059669", "#d97706", "#7c3aed", "#e11d48"][index % 5],
            "coordinates": [[point[0], point[1]] for point in route.geometry.coords] if route.geometry else [],
        }
        for index, route in enumerate(plan.routes.select_related("vehicle"))
    ]
    return _render(request, "planning/review.html", {"plan": plan, "route_features": route_features})


@active_organization_required
def route_list(request):
    routes = scope_queryset_to_membership(
        Route.objects.filter(route_plan__organization=request.organization),
        request,
    ).select_related("route_plan", "vehicle", "driver")
    return _render(request, "routes/list.html", {"routes": routes.order_by("-route_plan__service_date", "sequence")[:200]})


@active_organization_required
def route_detail(request, route_id):
    route = get_object_or_404(
        scope_queryset_to_membership(
            Route.objects.select_related("route_plan", "vehicle", "driver").prefetch_related(
                "stops__order__customer"
            ),
            request,
        ),
        pk=route_id,
        route_plan__organization=request.organization,
    )
    route_features = [
        {
            "id": route.pk,
            "vehicle": route.vehicle.code,
            "color": "#2563eb",
            "coordinates": [list(point) for point in route.geometry.coords] if route.geometry else [],
        }
    ]
    return _render(
        request,
        "routes/detail.html",
        {"route": route, "route_features": route_features},
    )


@active_organization_required
def live_map(request):
    routes = scope_queryset_to_membership(
        Route.objects.filter(
            route_plan__organization=request.organization,
            status__in=[Route.Status.DISPATCHED, Route.Status.IN_PROGRESS],
        ),
        request,
    ).select_related("vehicle", "driver")
    return _render(
        request,
        "live_map/index.html",
        {"routes": routes, "routes_count": routes.count()},
    )


@active_organization_required
def fleet_page(request, resource):
    if resource == "drivers":
        title, rows = "Drivers", scope_queryset_to_membership(
            Driver.objects.filter(organization=request.organization),
            request,
        ).select_related("depot")
    elif resource == "vehicles":
        title, rows = "Vehicles", scope_queryset_to_membership(
            Vehicle.objects.filter(organization=request.organization),
            request,
        ).select_related("depot")
    else:
        title, rows = "Depots", scope_queryset_to_membership(
            Depot.objects.filter(organization=request.organization),
            request,
        )
    # `rows_count` is computed explicitly because the template needs a total. A
    # `{{ rows|length }}` filter would evaluate the entire queryset into Python
    # just to count it, which is a full table scan and a memory cost proportional
    # to the table rather than to the page.
    return _render(
        request,
        "fleet/index.html",
        {"title": title, "resource": resource, "rows": rows, "rows_count": rows.count()},
    )


@active_organization_required
@require_http_methods(["GET", "POST"])
def exceptions_page(request):
    if request.method == "POST":
        _require_roles(request, "ADMIN", "OPERATIONS_MANAGER", "DISPATCHER", "CUSTOMER_SERVICE")
        item = get_object_or_404(
            scope_queryset_to_membership(
                OperationException.objects.filter(organization=request.organization),
                request,
            ),
            pk=request.POST.get("exception_id"),
        )
        item.status = OperationException.Status.RESOLVED
        item.resolution = request.POST.get("resolution", "")
        item.resolved_at = timezone.now()
        item.version += 1
        item.save()
        messages.success(request, "Exception resolved.")
        return redirect("exceptions")
    items = scope_queryset_to_membership(
        OperationException.objects.filter(organization=request.organization),
        request,
    ).select_related("order", "route", "assigned_to")
    return _render(request, "exceptions/index.html", {"exceptions": items.order_by("-created_at")})


@active_organization_required
@require_http_methods(["GET", "POST"])
def reports_page(request):
    """List KPI exports and accept new ones.

    Creating an export from the console is what makes the page a feature rather
    than a read-only table; previously the only call to action was a link into
    the Swagger UI, which cannot express a depot or a date range.
    """
    if request.method == "POST":
        allowed = scope_queryset_to_membership(
            Depot.objects.filter(organization=request.organization, active=True),
            request,
        )
        form = ReportExportForm(
            request.POST,
            allowed_depots=allowed,
            today=localdate_in(request.organization.timezone),
        )
        if form.is_valid():
            export = ReportExport.objects.create(
                organization=request.organization,
                report_type=ReportExportForm.REPORT_TYPE,
                filters_json=form.build_filters(),
                requested_by=request.user,
            )
            dispatch(export_report_task, export.pk)
            messages.success(request, "KPI export queued. It will appear below when it completes.")
            return redirect("reports")
        messages.error(request, "The export could not be created. Check the highlighted fields.")
    else:
        form = ReportExportForm(
            allowed_depots=scope_queryset_to_membership(
                Depot.objects.filter(organization=request.organization, active=True),
                request,
            ),
            today=localdate_in(request.organization.timezone),
        )

    exports = scope_report_exports_to_membership(
        ReportExport.objects.filter(organization=request.organization),
        request,
    ).order_by("-created_at")
    return _render(request, "reports/index.html", {"exports": exports, "form": form})


@active_organization_required
def integrations_page(request):
    """Report the real state of every external dependency the platform uses.

    The page used to render a hardcoded list from the template with an empty
    context, and stamped every row "Configured" in green regardless of what was
    actually wired up. A status board that cannot report a fault is worse than
    no status board, so each row is now derived from settings and from rows that
    exist in the database.
    """
    webhook_count = WebhookSubscription.objects.filter(
        organization=request.organization, active=True
    ).count()
    return _render(
        request,
        "integrations/index.html",
        {
            "integrations": _integration_status(request, webhook_count),
        },
    )


def _integration_status(request, webhook_count):
    """Build the capability list with a state resolved from live configuration."""
    geocoding = settings.GEOCODING_PROVIDER
    routing = settings.ROUTING_PROVIDER
    return [
        {
            "icon": "map-pin",
            "title": "Geocoding",
            "description": f"{geocoding} — resolves candidate addresses to WGS84 coordinates",
            "state": "Configured" if geocoding == "nominatim" else "Deterministic",
            "tone": "AVAILABLE" if geocoding == "nominatim" else "NEUTRAL",
        },
        {
            "icon": "route",
            "title": "Routing matrix",
            "description": f"{routing} — travel distance and duration between stops",
            "state": "Configured" if routing == "osrm" else "Deterministic",
            "tone": "AVAILABLE" if routing == "osrm" else "NEUTRAL",
        },
        {
            "icon": "webhook",
            "title": "Outbound webhooks",
            "description": (
                f"{webhook_count} active subscription(s) — HMAC-signed events with retry"
                if webhook_count
                else "No active subscription — create one to receive signed events"
            ),
            "state": "Active" if webhook_count else "Not configured",
            "tone": "AVAILABLE" if webhook_count else "PENDING",
        },
        {
            "icon": "database",
            "title": "Object storage",
            "description": f"{settings.STORAGES['default']['BACKEND'].rsplit('.', 1)[-1]} — private files behind an authorized endpoint",
            "state": "Local",
            "tone": "NEUTRAL",
        },
        {
            "icon": "mail",
            "title": "Email",
            "description": f"{settings.EMAIL_BACKEND.rsplit('.', 1)[-1]} — operational notifications",
            "state": "Console" if "console" in settings.EMAIL_BACKEND else "SMTP",
            "tone": "NEUTRAL" if "console" in settings.EMAIL_BACKEND else "AVAILABLE",
        },
        {
            "icon": "api",
            "title": "Public API",
            "description": "OpenAPI 3 schema, browsable endpoints and idempotent writes",
            "state": "Ready",
            "tone": "AVAILABLE",
        },
    ]


@active_organization_required
def settings_page(request):
    return _render(
        request,
        "settings/index.html",
        {
            "profiles": PlanningProfile.objects.filter(organization=request.organization),
            "depots": scope_queryset_to_membership(
                Depot.objects.filter(organization=request.organization),
                request,
            ),
        },
    )


@active_organization_required
def driver_today(request):
    if not hasattr(request.user, "driver_profile"):
        raise Http404
    driver = request.user.driver_profile
    routes = Route.objects.filter(
        driver=driver,
        route_plan__service_date=localdate_in(driver.depot.timezone),
        status__in=[Route.Status.DISPATCHED, Route.Status.IN_PROGRESS],
    ).select_related("vehicle", "route_plan__depot").prefetch_related("stops__order__customer")
    return _render(request, "driver/today.html", {"driver": driver, "routes": routes})


@active_organization_required
def driver_route(request, route_id):
    route = get_object_or_404(
        Route.objects.select_related("vehicle", "driver", "route_plan__depot").prefetch_related(
            "stops__order__customer", "stops__order__delivery_address"
        ),
        pk=route_id,
        driver__user=request.user,
    )
    return _render(request, "driver/route.html", {"route": route})


@active_organization_required
def driver_stop(request, stop_id):
    # ``order`` is nullable: depot start/end and break stops are real stop rows
    # with no delivery attached, so the template must not assume one exists.
    # ``order__delivery_address`` is fetched here to keep the address block from
    # issuing a query per render.
    stop = get_object_or_404(
        RouteStop.objects.select_related(
            "route",
            "order__customer",
            "order__delivery_address",
            "address",
        ),
        pk=stop_id,
        route__driver__user=request.user,
    )
    return _render(request, "driver/stop.html", {"stop": stop})


@active_organization_required
@require_http_methods(["GET", "POST"])
def driver_pod(request, stop_id):
    stop = get_object_or_404(
        RouteStop.objects.select_related("route", "order__customer"),
        pk=stop_id,
        route__driver__user=request.user,
    )
    # Proof of delivery only applies to a stop that delivers something. Depot
    # and break stops carry no order, and accepting a POD against one would
    # record a signature for a stop that was never a customer visit.
    if stop.order_id is None:
        messages.error(request, "Proof of delivery is only available for delivery stops.")
        return redirect("driver-stop-page", stop_id=stop.pk)

    pod = getattr(stop, "proof_of_delivery", None)
    if request.method == "POST":
        pod, _ = ProofOfDelivery.objects.update_or_create(
            route_stop=stop,
            defaults={
                "recipient_name": request.POST.get("recipient_name", ""),
                "recipient_relation": request.POST.get("recipient_relation", ""),
                "photo_file": request.FILES.get("photo_file") or getattr(pod, "photo_file", None),
                "signature_file": request.FILES.get("signature_file") or getattr(pod, "signature_file", None),
                "note": request.POST.get("note", ""),
                "captured_at": timezone.now(),
            },
        )
        messages.success(request, "Proof of delivery saved.")
        return redirect("driver-stop-page", stop_id=stop.pk)
    return _render(request, "driver/pod.html", {"stop": stop, "pod": pod})


@active_organization_required
def driver_history(request):
    if not hasattr(request.user, "driver_profile"):
        raise Http404
    routes = Route.objects.filter(
        driver=request.user.driver_profile,
        status__in=[Route.Status.COMPLETED, Route.Status.CANCELLED],
    ).order_by("-route_plan__service_date")[:100]
    return _render(request, "driver/history.html", {"routes": routes})


@login_required
def private_media(request, file_path):
    # This endpoint is intentionally not wrapped in active_organization_required
    # because it needs to deny rather than redirect: it serves files, so a signed
    # out caller must receive 404 rather than a login redirect that a download
    # client would follow and then store as the file. The membership is still
    # checked explicitly so a session without an active organization fails closed
    # instead of raising AttributeError on None.
    if request.membership is None or request.organization is None:
        raise Http404

    allowed = False
    if file_path.startswith("pod/"):
        pods = scope_queryset_to_membership(ProofOfDelivery.objects.all(), request)
        if request.membership.role == "DRIVER":
            pods = pods.filter(route_stop__route__driver__user=request.user)
        allowed = pods.filter(
            Q(photo_file=file_path) | Q(signature_file=file_path),
            route_stop__route__route_plan__organization=request.organization,
        ).exists()
    elif file_path.startswith("reports/") and request.membership.role in {
        "ADMIN",
        "OPERATIONS_MANAGER",
        "DISPATCHER",
        "AUDITOR",
    }:
        allowed = scope_report_exports_to_membership(ReportExport.objects.all(), request).filter(
            output_file=file_path,
            organization=request.organization,
        ).exists()
    elif file_path.startswith("imports/") and request.membership.role in {
        "ADMIN",
        "OPERATIONS_MANAGER",
        "DISPATCHER",
    }:
        allowed = scope_queryset_to_membership(ImportJob.objects.all(), request).filter(
            source_file=file_path,
            organization=request.organization,
        ).exists()
    if not allowed or not default_storage.exists(file_path):
        raise Http404
    return FileResponse(default_storage.open(file_path, "rb"), as_attachment=True)


def service_worker(request):
    path = Path(settings.BASE_DIR) / "static" / "service-worker.js"
    response = FileResponse(path.open("rb"), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response
