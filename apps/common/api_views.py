
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework import serializers as drf_serializers
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.audit.services import begin_idempotent, complete_idempotent, record_audit
from apps.common.dispatch import dispatch
from apps.common.events import async_event_stream, event_stream
from apps.common.fields import EncryptedTextField
from apps.common.middleware import resolve_active_organization
from apps.common.permissions import HasOrganization, IsDriver
from apps.common.scoping import scope_queryset_to_membership, scope_report_exports_to_membership
from apps.common.serializers import (
    AddressSerializer,
    CustomerSerializer,
    DepotSerializer,
    DriverSerializer,
    DriverShiftSerializer,
    DriverTokenSerializer,
    ImportJobSerializer,
    OperationExceptionSerializer,
    OrderSerializer,
    PlanningProfileSerializer,
    PlanningRunCreateSerializer,
    PlanningRunSerializer,
    ProofOfDeliverySerializer,
    ReportExportSerializer,
    RestrictedZoneSerializer,
    RoutePlanSerializer,
    RouteSerializer,
    RouteStopSerializer,
    VehicleSerializer,
    WebhookSubscriptionSerializer,
)
from apps.common.state_machine import ORDER_TRANSITIONS, assert_transition
from apps.common.time import localdate_in
from apps.customers.models import Address, Customer
from apps.depots.models import Depot, RestrictedZone
from apps.dispatch.services import dispatch_plan
from apps.drivers.models import Driver, DriverDevice, DriverShift
from apps.exceptions.models import OperationException
from apps.fleet.models import Vehicle
from apps.integrations.models import WebhookSubscription
from apps.optimization.services import optimize_run
from apps.optimization.tasks import optimize_run_task
from apps.orders.models import ImportJob, Order
from apps.orders.tasks import geocode_order_task, import_orders_task
from apps.planning.models import PlanningProfile, PlanningRun, Route, RoutePlan, RouteStop
from apps.planning.services import clone_plan, move_stop, recalculate_route, validate_plan
from apps.proof_of_delivery.models import ProofOfDelivery
from apps.reports.models import ReportExport
from apps.reports.tasks import export_report_task
from apps.routing.services import build_matrix
from apps.routing.tasks import build_matrix_task, geocode_address_task
from apps.tracking.models import DriverEvent, VehicleLocation
from apps.tracking.services import DriverActionConflict, record_driver_action, record_location


class DriverTokenView(TokenObtainPairView):
    serializer_class = DriverTokenSerializer


class GenericPayloadSerializer(drf_serializers.Serializer):
    pass


def _audit_snapshot(instance):
    """Concrete field values of ``instance``, for the audit before/after columns.

    Only fields with a loaded value are included, so this never triggers a query
    per deferred field and never records an unrelated relation. Decrypted PII
    columns are excluded on purpose: the audit log is read by auditors through a
    wider surface than the field itself, and a log must not become a plaintext
    copy of everything it recorded.
    """
    values = {}
    for field in instance._meta.concrete_fields:
        if isinstance(field, EncryptedTextField) or field.primary_key:
            continue
        value = instance.__dict__.get(field.attname)
        if value is not None:
            values[field.attname] = value
    return values


class DocumentedAPIView(APIView):
    serializer_class = GenericPayloadSerializer

    def initial(self, request, *args, **kwargs):
        # DRF performs token authentication here, after middleware has already
        # run. Resolving the organization again means bearer-token callers get a
        # correct ``request.organization`` before permissions are checked, which
        # is what IsDriver and HasOrganization rely on.
        resolve_active_organization(request)
        super().initial(request, *args, **kwargs)


class OrganizationViewSetMixin(DocumentedAPIView):
    """Base for every organization-scoped endpoint.

    Inheriting ``DocumentedAPIView`` is load-bearing, not cosmetic.
    ``CurrentOrganizationMiddleware`` deliberately skips bearer requests because
    it runs before DRF has authenticated anyone, which leaves the organization
    unresolved at permission-check time. Only ``DocumentedAPIView.initial``
    finishes the job. When the ViewSets inherited plain ``viewsets.*`` instead,
    ``request.organization`` stayed ``None``, ``HasOrganization`` denied every
    request, and all sixteen router endpoints answered 403 to the very clients
    the token endpoint had just authenticated. Anything exposing an
    organization-scoped ViewSet must derive from this mixin.
    """

    permission_classes = [IsAuthenticated, HasOrganization]
    write_roles = {"ADMIN", "OPERATIONS_MANAGER", "DISPATCHER"}

    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            membership = getattr(request, "membership", None)
            if not membership or membership.role not in self.write_roles:
                raise PermissionDenied("Your organization role does not allow this change.")

    def scope_queryset(self, queryset):
        if any(field.name == "organization" for field in queryset.model._meta.fields):
            queryset = queryset.filter(organization=self.request.organization)
        return scope_queryset_to_membership(queryset, self.request)

    def get_queryset(self):
        return self.scope_queryset(super().get_queryset())


class OptimisticVersionMixin:
    def _check_if_match(self, instance):
        expected = self.request.headers.get("If-Match")
        if not expected:
            raise ValidationError({"If-Match": "This header is required for updates."})
        normalized = expected.strip()
        if normalized.startswith("W/"):
            normalized = normalized[2:].strip()
        normalized = normalized.removeprefix('"').removesuffix('"')
        if normalized != str(instance.version):
            raise ValidationError({"If-Match": f"Version conflict; current version is {instance.version}."})

    def _get_locked_object(self):
        queryset = self.filter_queryset(self.get_queryset()).select_related(None).select_for_update()
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        filter_kwargs = {self.lookup_field: self.kwargs[lookup_url_kwarg]}
        instance = get_object_or_404(queryset, **filter_kwargs)
        self.check_object_permissions(self.request, instance)
        return instance

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self._get_locked_object()
        self._check_if_match(instance)
        before = _audit_snapshot(instance)
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        instance.version += 1
        instance.save(update_fields=["version", "updated_at"])
        instance.refresh_from_db()
        # Versioned writes are the generic mutation path for every updatable
        # resource, so this is the single place their audit trail is recorded
        # rather than one call per ViewSet that a new resource could forget.
        record_audit(
            organization=request.organization,
            actor=request.user,
            action=f"{instance._meta.model_name}.updated",
            instance=instance,
            before=before,
            after=_audit_snapshot(instance),
            request=request,
        )
        response = Response(self.get_serializer(instance).data)
        response["ETag"] = f'W/"{instance.version}"'
        return response

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        instance = self._get_locked_object()
        self._check_if_match(instance)
        before = _audit_snapshot(instance)
        resource_type = instance._meta.label_lower
        resource_id = instance.pk
        self.perform_destroy(instance)
        # Captured before the delete: Django clears instance.pk, so the log has
        # to be told which row it belongs to.
        record_audit(
            organization=request.organization,
            actor=request.user,
            action=f"{instance._meta.model_name}.deleted",
            instance=instance,
            before=before,
            request=request,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        instance = self.get_object()
        response["ETag"] = f'W/"{instance.version}"'
        return response


class DepotViewSet(OrganizationViewSetMixin, viewsets.ModelViewSet):
    write_roles = {"ADMIN", "OPERATIONS_MANAGER"}
    queryset = Depot.objects.all()
    serializer_class = DepotSerializer
    filterset_fields = ["active", "code"]
    search_fields = ["name", "code"]


class RestrictedZoneViewSet(OrganizationViewSetMixin, viewsets.ModelViewSet):
    write_roles = {"ADMIN", "OPERATIONS_MANAGER"}
    queryset = RestrictedZone.objects.all()
    serializer_class = RestrictedZoneSerializer


class CustomerViewSet(OrganizationViewSetMixin, viewsets.ModelViewSet):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    filterset_fields = ["status"]
    search_fields = ["name", "external_ref"]


class AddressViewSet(OrganizationViewSetMixin, viewsets.ModelViewSet):
    queryset = Address.objects.select_related("customer")
    serializer_class = AddressSerializer
    filterset_fields = ["geocode_status", "country_code", "city"]

    @action(detail=True, methods=["post"])
    def geocode(self, request, pk=None):
        address = self.get_object()
        if request.query_params.get("sync") == "1":
            from apps.routing.services import geocode_address

            geocode_address(address)
            return Response(self.get_serializer(address).data)
        task = dispatch(geocode_address_task, address.pk)
        return Response({"task_id": task.id, "status": "QUEUED"}, status=status.HTTP_202_ACCEPTED)


class VehicleViewSet(OrganizationViewSetMixin, OptimisticVersionMixin, viewsets.ModelViewSet):
    write_roles = {"ADMIN", "OPERATIONS_MANAGER", "FLEET_MANAGER"}
    queryset = Vehicle.objects.select_related("depot")
    serializer_class = VehicleSerializer
    filterset_fields = ["depot", "status", "vehicle_type", "active"]


class DriverViewSet(OrganizationViewSetMixin, OptimisticVersionMixin, viewsets.ModelViewSet):
    write_roles = {"ADMIN", "OPERATIONS_MANAGER", "FLEET_MANAGER"}
    queryset = Driver.objects.select_related("depot", "user")
    serializer_class = DriverSerializer
    filterset_fields = ["depot", "status"]


class DriverShiftViewSet(OrganizationViewSetMixin, viewsets.ModelViewSet):
    # Routed through OrganizationViewSetMixin rather than re-implementing its
    # permission and write-role checks. This ViewSet previously derived from
    # plain viewsets.ModelViewSet, so it missed both the organization
    # resolution that bearer requests depend on and the defensive
    # `request.membership` guard, and answered 403 to every driver token.
    write_roles = {"ADMIN", "OPERATIONS_MANAGER", "FLEET_MANAGER"}
    serializer_class = DriverShiftSerializer
    queryset = DriverShift.objects.select_related("driver")

    def get_queryset(self):
        # DriverShift has no organization column of its own; the chain through
        # driver is what ties a shift to an organization.
        queryset = DriverShift.objects.select_related("driver").filter(
            driver__organization=self.request.organization
        )
        return scope_queryset_to_membership(queryset, self.request)


class OrderViewSet(OrganizationViewSetMixin, OptimisticVersionMixin, viewsets.ModelViewSet):
    # `items` and `time_windows` are reverse relations read by
    # OrderSerializer. select_related covers the forward FKs; without an
    # explicit prefetch on the reverse side, serializing one page of 50 orders
    # issued 100 additional queries — two per row.
    queryset = Order.objects.select_related(
        "customer", "delivery_address", "pickup_address", "depot"
    ).prefetch_related("items", "time_windows")
    serializer_class = OrderSerializer
    filterset_fields = ["service_date", "depot", "status", "priority", "order_type"]
    search_fields = ["external_ref", "customer__name", "delivery_address__formatted_address"]

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise ValidationError({"Idempotency-Key": "This header is required."})
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = begin_idempotent(
                organization=request.organization,
                actor_key=str(request.user.pk),
                operation="order.create",
                key=key,
                payload=request.data,
            )
        except ValueError as exc:
            raise ValidationError({"Idempotency-Key": str(exc)}) from exc
        if result.replayed:
            return Response(result.record.response_json, status=result.record.response_status)
        self.perform_create(serializer)
        response = Response(
            serializer.data,
            status=status.HTTP_201_CREATED,
            headers=self.get_success_headers(serializer.data),
        )
        complete_idempotent(result.record, response.status_code, response.data)
        order = serializer.instance
        if order.status == Order.Status.DRAFT:
            transaction.on_commit(lambda: dispatch(geocode_order_task, order.pk))
        return response

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def cancel(self, request, pk=None):
        order = self._get_locked_object()
        self._check_if_match(order)
        assert_transition(order.status, Order.Status.CANCELLED, ORDER_TRANSITIONS, entity="Order")
        order.status = Order.Status.CANCELLED
        order.cancelled_at = timezone.now()
        order.version += 1
        order.save(update_fields=["status", "cancelled_at", "version", "updated_at"])
        record_audit(
            organization=request.organization,
            actor=request.user,
            action="order.cancelled",
            instance=order,
            after={"status": order.status},
            request=request,
        )
        return Response(self.get_serializer(order).data)

    @action(detail=True, methods=["post"])
    def geocode(self, request, pk=None):
        order = self.get_object()
        if request.query_params.get("sync") == "1":
            geocode_order_task.run(order.pk)
            order.refresh_from_db()
            return Response(self.get_serializer(order).data)
        task = dispatch(geocode_order_task, order.pk)
        return Response({"task_id": task.id, "status": "QUEUED"}, status=status.HTTP_202_ACCEPTED)

    @action(
        detail=False,
        methods=["post"],
        url_path="import",
        parser_classes=[MultiPartParser, FormParser],
    )
    def import_csv(self, request):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise ValidationError({"Idempotency-Key": "This header is required."})
        serializer = ImportJobSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        source = serializer.validated_data["source_file"]
        try:
            result = begin_idempotent(
                organization=request.organization,
                actor_key=str(request.user.pk),
                operation="order.import",
                key=key,
                payload={
                    "depot": serializer.validated_data["depot"].pk,
                    "source_file": source.name,
                    "source_size": source.size,
                },
            )
        except ValueError as exc:
            raise ValidationError({"Idempotency-Key": str(exc)}) from exc
        if result.replayed:
            return Response(result.record.response_json, status=result.record.response_status)
        job = serializer.save(organization=request.organization, requested_by=request.user)
        task = dispatch(import_orders_task, job.pk)
        payload = {"id": job.pk, "task_id": task.id, "status": job.status}
        complete_idempotent(result.record, status.HTTP_202_ACCEPTED, payload)
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class ImportJobViewSet(OrganizationViewSetMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = ImportJob.objects.prefetch_related("row_errors")
    serializer_class = ImportJobSerializer

    @action(detail=True, methods=["get"], url_path="errors")
    def errors(self, request, pk=None):
        job = self.get_object()
        return Response(
            [
                {"row_number": item.row_number, "code": item.code, "message": item.message, "raw_data": item.raw_data}
                for item in job.row_errors.all()
            ]
        )


class PlanningProfileViewSet(OrganizationViewSetMixin, OptimisticVersionMixin, viewsets.ModelViewSet):
    write_roles = {"ADMIN", "OPERATIONS_MANAGER"}
    queryset = PlanningProfile.objects.all()
    serializer_class = PlanningProfileSerializer


class PlanningRunViewSet(OrganizationViewSetMixin, viewsets.ModelViewSet):
    queryset = PlanningRun.objects.select_related("depot", "profile", "requested_by")
    http_method_names = ["get", "post", "head", "options"]

    def get_serializer_class(self):
        return PlanningRunCreateSerializer if self.action == "create" else PlanningRunSerializer

    @staticmethod
    def _ensure_plannable(run):
        if run.status in {
            PlanningRun.Status.DISPATCHED,
            PlanningRun.Status.IN_PROGRESS,
            PlanningRun.Status.COMPLETED,
            PlanningRun.Status.CANCELLED,
        }:
            raise ValidationError(f"Planning run cannot be changed from status {run.status}.")

    @action(detail=True, methods=["post"], url_path="build-matrix")
    def build_matrix_action(self, request, pk=None):
        run = self.get_object()
        self._ensure_plannable(run)
        if request.query_params.get("sync") == "1":
            matrix = build_matrix(run.pk)
            run.refresh_from_db(fields=["status"])
            return Response({"matrix_id": matrix.pk if matrix else None, "status": run.status})
        task = dispatch(build_matrix_task, run.pk)
        PlanningRun.objects.filter(pk=run.pk).update(celery_task_id=task.id)
        return Response({"task_id": task.id, "status": "MATRIX_PENDING"}, status=202)

    @action(detail=True, methods=["post"])
    def optimize(self, request, pk=None):
        run = self.get_object()
        self._ensure_plannable(run)
        if request.query_params.get("sync") == "1":
            plan = optimize_run(run.pk)
            return Response(RoutePlanSerializer(plan).data)
        task = dispatch(optimize_run_task, run.pk)
        PlanningRun.objects.filter(pk=run.pk).update(celery_task_id=task.id)
        return Response({"task_id": task.id, "status": "OPTIMIZING"}, status=202)

    @action(detail=True, methods=["get"])
    def progress(self, request, pk=None):
        run = self.get_object()
        if "text/event-stream" not in request.headers.get("Accept", ""):
            return Response(
                {
                    "id": run.pk,
                    "version": run.version,
                    "status": run.status,
                    "percent": run.progress_percent,
                    "phase": run.current_phase,
                    "message": run.progress_message,
                }
            )
        django_request = getattr(request, "_request", request)
        stream = (
            async_event_stream(f"planning:{run.pk}")
            if hasattr(django_request, "scope")
            else event_stream(f"planning:{run.pk}")
        )
        response = StreamingHttpResponse(stream, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


class RoutePlanViewSet(OrganizationViewSetMixin, OptimisticVersionMixin, viewsets.ReadOnlyModelViewSet):
    queryset = RoutePlan.objects.prefetch_related(
        "routes__stops__order", "violations", "unassigned_orders__order"
    )
    serializer_class = RoutePlanSerializer
    http_method_names = ["get", "post", "head", "options"]

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        plan = self.get_object()
        violations = validate_plan(plan)
        return Response({"valid": not violations.filter(severity="ERROR").exists(), "violations": list(violations.values())})

    # The Python method cannot be called `dispatch`: that name belongs to
    # View.dispatch, the method DRF calls to route every inbound HTTP request.
    # Shadowing it made this ViewSet unreachable — the list endpoint, retrieve,
    # validate and clone all raised AssertionError because they arrived here
    # with no `pk`. The action keeps the `dispatch` URL path and reverse name
    # through url_path/url_name, so `/api/v1/route-plans/{pk}/dispatch/` and
    # existing clients are unaffected.
    @action(detail=True, methods=["post"], url_path="dispatch", url_name="dispatch")
    def commit_dispatch(self, request, pk=None):
        current = self.get_object()
        self._check_if_match(current)
        try:
            plan = dispatch_plan(
                current.pk,
                request.user,
                request,
                expected_version=current.version,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(self.get_serializer(plan).data)

    @action(detail=True, methods=["post"])
    def clone(self, request, pk=None):
        current = self.get_object()
        self._check_if_match(current)
        try:
            cloned, _, _ = clone_plan(current, request.user, expected_version=current.version)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(self.get_serializer(cloned).data, status=201)


class RouteViewSet(OrganizationViewSetMixin, OptimisticVersionMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Route.objects.select_related("route_plan", "vehicle", "driver").prefetch_related("stops__order")
    serializer_class = RouteSerializer
    http_method_names = ["get", "post", "head", "options"]

    def scope_queryset(self, queryset):
        return scope_queryset_to_membership(
            queryset.filter(route_plan__organization=self.request.organization),
            self.request,
        )

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def recalculate(self, request, pk=None):
        route = self._get_locked_object()
        self._check_if_match(route)
        if route.route_plan.status in {RoutePlan.Status.DISPATCHED, RoutePlan.Status.IN_PROGRESS, RoutePlan.Status.COMPLETED}:
            raise ValidationError("Dispatched routes are immutable.")
        recalculate_route(route)
        validate_plan(route.route_plan)
        return Response(self.get_serializer(route).data)


class RouteStopViewSet(OrganizationViewSetMixin, OptimisticVersionMixin, viewsets.ReadOnlyModelViewSet):
    queryset = RouteStop.objects.select_related("route__route_plan", "order", "address")
    serializer_class = RouteStopSerializer
    http_method_names = ["get", "post", "head", "options"]

    def scope_queryset(self, queryset):
        return scope_queryset_to_membership(
            queryset.filter(route__route_plan__organization=self.request.organization),
            self.request,
        )

    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        stop = self.get_object()
        self._check_if_match(stop)
        target_route_id = request.data.get("target_route_id")
        target_sequence = request.data.get("target_sequence")
        if target_route_id is None or target_sequence is None:
            raise ValidationError("target_route_id and target_sequence are required.")
        try:
            plan = move_stop(
                plan=stop.route.route_plan,
                stop_id=stop.pk,
                target_route_id=int(target_route_id),
                target_sequence=int(target_sequence),
                actor=request.user,
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc
        return Response(RoutePlanSerializer(plan).data, status=201)


class ExceptionViewSet(OrganizationViewSetMixin, OptimisticVersionMixin, viewsets.ModelViewSet):
    write_roles = {"ADMIN", "OPERATIONS_MANAGER", "DISPATCHER", "CUSTOMER_SERVICE"}
    queryset = OperationException.objects.select_related("order", "route", "assigned_to")
    serializer_class = OperationExceptionSerializer
    filterset_fields = ["status", "severity", "type"]

    def perform_create(self, serializer):
        serializer.save(reported_by=self.request.user)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def resolve(self, request, pk=None):
        instance = self._get_locked_object()
        self._check_if_match(instance)
        resolution = str(request.data.get("resolution", "")).strip()
        if not resolution:
            raise ValidationError({"resolution": "This field is required."})
        if instance.status == OperationException.Status.RESOLVED:
            raise ValidationError("This exception is already resolved.")
        before = _audit_snapshot(instance)
        instance.status = OperationException.Status.RESOLVED
        instance.resolution = resolution
        instance.resolved_at = timezone.now()
        instance.version += 1
        instance.save(update_fields=["status", "resolution", "resolved_at", "version", "updated_at"])
        # Resolution closes an operational incident, so the written explanation
        # and who wrote it belong in the audit trail alongside the state change.
        record_audit(
            organization=request.organization,
            actor=request.user,
            action="exception.resolved",
            instance=instance,
            before=before,
            after=_audit_snapshot(instance),
            request=request,
        )
        return Response(self.get_serializer(instance).data)


class ReportExportViewSet(OrganizationViewSetMixin, viewsets.ModelViewSet):
    write_roles = {"ADMIN", "OPERATIONS_MANAGER", "DISPATCHER"}
    queryset = ReportExport.objects.all()
    serializer_class = ReportExportSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return scope_report_exports_to_membership(super().get_queryset(), self.request)

    def perform_create(self, serializer):
        export = serializer.save(requested_by=self.request.user)
        dispatch(export_report_task, export.pk)


class WebhookSubscriptionViewSet(OrganizationViewSetMixin, viewsets.ModelViewSet):
    write_roles = {"ADMIN"}
    queryset = WebhookSubscription.objects.all()
    serializer_class = WebhookSubscriptionSerializer


class DashboardAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, HasOrganization]

    def get(self, request):
        today = localdate_in(request.organization.timezone)
        orders = Order.objects.filter(organization=request.organization, service_date=today)
        orders = scope_queryset_to_membership(orders, request)
        counts = dict(orders.values_list("status").annotate(count=Count("id")))
        active_routes = Route.objects.filter(
            route_plan__organization=request.organization,
            route_plan__service_date=today,
        )
        active_routes = scope_queryset_to_membership(active_routes, request)
        completed = counts.get(Order.Status.COMPLETED, 0)
        failed = counts.get(Order.Status.FAILED, 0)
        attempted = completed + failed
        open_exceptions = scope_queryset_to_membership(
            OperationException.objects.filter(
                organization=request.organization,
                status=OperationException.Status.OPEN,
            ),
            request,
        ).count()
        return Response(
            {
                "date": today,
                "orders_today": orders.count(),
                "planned": counts.get(Order.Status.PLANNED, 0),
                "unassigned": counts.get(Order.Status.UNASSIGNED, 0),
                "in_progress": counts.get(Order.Status.IN_PROGRESS, 0),
                "completed": completed,
                "failed": failed,
                "first_attempt_success_percent": round(completed / attempted * 100, 1) if attempted else None,
                "planned_distance_m": sum(active_routes.values_list("total_distance_m", flat=True)),
                "vehicles_used": active_routes.values("vehicle_id").distinct().count(),
                "open_exceptions": open_exceptions,
            }
        )


def _driver(request):
    try:
        driver = request.user.driver_profile
    except Driver.DoesNotExist as exc:
        raise PermissionDenied("No driver profile is associated with this account.") from exc
    if driver.organization_id != request.organization.id:
        raise PermissionDenied("Driver does not belong to the active organization.")
    return driver


class DriverRoutesTodayAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, IsDriver]

    def get(self, request):
        driver = _driver(request)
        routes = Route.objects.filter(
            driver=driver,
            route_plan__service_date=localdate_in(driver.depot.timezone),
            status__in=[Route.Status.DISPATCHED, Route.Status.IN_PROGRESS],
        ).select_related(
            "vehicle", "driver", "route_plan__depot"
        ).prefetch_related(
            "stops__order__delivery_address"
        )
        # select_related on vehicle/driver/route_plan and a prefetch down to
        # delivery_address are required by RouteSerializer and
        # RouteStopSerializer. Without them this — the most frequently polled
        # endpoint in the product — issued three extra queries per route.
        return Response(RouteSerializer(routes, many=True).data)


class DriverRouteAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, IsDriver]

    def get(self, request, route_id):
        route = get_object_or_404(
            Route.objects.select_related(
                "vehicle", "driver", "route_plan__depot"
            ).prefetch_related(
                "stops__order__delivery_address"
            ),
            pk=route_id,
            driver=_driver(request),
        )
        return Response(RouteSerializer(route).data)


class DriverRouteStartAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, IsDriver]

    def post(self, request, route_id):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise ValidationError({"Idempotency-Key": "This header is required."})
        driver = _driver(request)
        get_object_or_404(Route, pk=route_id, driver=driver)
        try:
            event, replayed = record_driver_action(
                driver=driver,
                route_id=route_id,
                event_type="ROUTE_STARTED",
                idempotency_key=key,
                payload=request.data,
            )
        except DriverActionConflict as exc:
            return Response({"code": "conflict", "message": str(exc)}, status=409)
        return Response({"event_id": event.pk, "replayed": replayed})


class DriverStopActionAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, IsDriver]
    action_map = {"arrive": "STOP_ARRIVED", "complete": "STOP_COMPLETED", "fail": "STOP_FAILED"}

    def post(self, request, stop_id, operation):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise ValidationError({"Idempotency-Key": "This header is required."})
        event_type = self.action_map.get(operation)
        if event_type is None:
            raise ValidationError({"operation": "Use arrive, complete, or fail."})
        stop = get_object_or_404(RouteStop.objects.select_related("route"), pk=stop_id, route__driver=_driver(request))
        try:
            event, replayed = record_driver_action(
                driver=_driver(request),
                route_id=stop.route_id,
                stop_id=stop.pk,
                event_type=event_type,
                idempotency_key=key,
                payload=request.data,
            )
        except DriverActionConflict as exc:
            return Response({"code": "conflict", "message": str(exc)}, status=409)
        stop.refresh_from_db()
        return Response({"event_id": event.pk, "replayed": replayed, "stop": RouteStopSerializer(stop).data})


class DriverPODAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, IsDriver]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, stop_id):
        stop = get_object_or_404(RouteStop, pk=stop_id, route__driver=_driver(request))
        serializer = ProofOfDeliverySerializer(data={**request.data, "route_stop": stop.pk})
        serializer.is_valid(raise_exception=True)
        pod, created = ProofOfDelivery.objects.update_or_create(
            route_stop=stop,
            defaults=serializer.validated_data | {"captured_at": serializer.validated_data.get("captured_at", timezone.now())},
        )
        return Response(ProofOfDeliverySerializer(pod).data, status=201 if created else 200)


class DriverLocationAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, IsDriver]
    throttle_scope = "driver_location"

    def post(self, request):
        driver = _driver(request)
        route = get_object_or_404(Route, pk=request.data.get("route_id"), driver=driver)
        try:
            location = record_location(driver=driver, route=route, payload=request.data)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return Response({"id": location.pk, "recorded_at": location.recorded_at}, status=201)


class DriverOfflineBatchAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, IsDriver]

    def post(self, request):
        driver = _driver(request)
        events = request.data.get("events", [])
        if not isinstance(events, list):
            raise ValidationError({"events": "Must be a list."})
        if len(events) > 100:
            raise ValidationError({"events": "At most 100 events can be synchronized at once."})
        if any(not isinstance(item, dict) for item in events):
            raise ValidationError({"events": "Every event must be an object."})
        def sequence(item):
            try:
                return int(item.get("sequence", 0))
            except (TypeError, ValueError):
                return 0

        results = []
        for item in sorted(events, key=sequence):
            try:
                event, replayed = record_driver_action(
                    driver=driver,
                    event_type=item["event_type"],
                    idempotency_key=item["idempotency_key"],
                    route_id=item["route_id"],
                    stop_id=item.get("stop_id"),
                    payload=item.get("payload", {}) | {"expected_version": item.get("expected_version")},
                    occurred_at=item.get("occurred_at"),
                )
                results.append({"idempotency_key": item["idempotency_key"], "status": "accepted", "event_id": event.pk, "replayed": replayed})
            except (KeyError, ValueError, DriverActionConflict) as exc:
                results.append({"idempotency_key": item.get("idempotency_key"), "status": "rejected", "message": str(exc)})
        return Response({"results": results})


class DriverDeviceRevokeAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, IsDriver]

    def post(self, request, device_id):
        device = get_object_or_404(DriverDevice, driver=_driver(request), device_id=device_id)
        device.revoked_at = timezone.now()
        device.save(update_fields=["revoked_at", "updated_at"])
        return Response(status=204)


class LiveRoutesAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, HasOrganization]

    def get(self, request):
        routes = Route.objects.filter(
            route_plan__organization=request.organization,
            status__in=[Route.Status.DISPATCHED, Route.Status.IN_PROGRESS],
        ).select_related("vehicle", "driver")
        routes = scope_queryset_to_membership(routes, request)
        features = []
        for route in routes:
            if route.geometry:
                features.append(
                    {
                        "type": "Feature",
                        "geometry": route.geometry.geojson and __import__("json").loads(route.geometry.geojson),
                        "properties": {
                            "route_id": route.pk,
                            "vehicle": route.vehicle.code,
                            "driver": route.driver.full_name if route.driver else None,
                            "status": route.status,
                        },
                    }
                )
        return Response({"type": "FeatureCollection", "features": features})


class LiveVehiclesAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, HasOrganization]

    def get(self, request):
        # One query for every vehicle's latest position. A per-vehicle
        # `.first()` is N+1, and pulling the full location history per vehicle is
        # worse: it transfers every ping ever recorded for the whole fleet on
        # every poll. The correlated subquery returns only the newest row.
        newest_location = (
            VehicleLocation.objects.filter(vehicle_id=OuterRef("pk"))
            .order_by("-recorded_at", "-id")
            .values("id")[:1]
        )
        vehicles = list(
            scope_queryset_to_membership(
                Vehicle.objects.filter(organization=request.organization, active=True),
                request,
            )
            .annotate(location_id=Subquery(newest_location))
            .values("pk", "code", "location_id")
        )
        if not vehicles:
            return Response({"type": "FeatureCollection", "features": []})

        locations = {
            location.pk: location
            for location in VehicleLocation.objects.filter(
                pk__in=[row["location_id"] for row in vehicles if row["location_id"]]
            ).select_related("driver")
        }
        now = timezone.now()
        features = []
        for row in vehicles:
            location = locations.get(row["location_id"])
            if not location:
                continue
            age = int((now - location.recorded_at).total_seconds())
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [location.location.x, location.location.y],
                    },
                    "properties": {
                        "vehicle_id": row["pk"],
                        "vehicle": row["code"],
                        "driver": location.driver.full_name if location.driver else None,
                        "route_id": location.route_id,
                        "recorded_at": location.recorded_at,
                        "stale": age > 120,
                    },
                }
            )
        return Response({"type": "FeatureCollection", "features": features})


class RouteEventsAPIView(DocumentedAPIView):
    permission_classes = [IsAuthenticated, HasOrganization]

    def get(self, request, route_id):
        get_object_or_404(
            scope_queryset_to_membership(
                Route.objects.filter(route_plan__organization=request.organization),
                request,
            ),
            pk=route_id,
        )
        queryset = DriverEvent.objects.filter(route_id=route_id).order_by("-pk")
        before = request.query_params.get("before")
        if before:
            try:
                before = int(before)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"before": "Must be an integer cursor."}) from exc
            queryset = queryset.filter(pk__lt=before)
        data = list(
            queryset[:100].values(
                "id", "event_type", "occurred_at", "driver_id", "route_stop_id", "payload_json"
            )
        )
        return Response({"results": data, "next_cursor": data[-1]["id"] if data else None})
