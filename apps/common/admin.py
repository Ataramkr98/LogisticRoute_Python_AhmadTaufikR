from django.conf import settings
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.gis.admin import GISModelAdmin
from django_otp.admin import OTPAdminSite

from apps.accounts.models import User
from apps.audit.models import AuditLog, IdempotencyRecord
from apps.customers.models import Address, Customer, GeocodeCache
from apps.depots.models import Depot, RestrictedZone
from apps.drivers.models import Driver, DriverDevice, DriverShift
from apps.exceptions.models import OperationException
from apps.fleet.models import Vehicle, VehicleAvailability
from apps.integrations.models import Notification, WebhookDelivery, WebhookSubscription
from apps.orders.models import ImportJob, ImportRowError, Order, OrderItem, OrderTimeWindow
from apps.organizations.models import Organization, OrganizationMembership
from apps.planning.models import (
    DistanceMatrix,
    PlanningProfile,
    PlanningRun,
    PlanningRunOrder,
    PlanningRunVehicle,
    Route,
    RoutePlan,
    RouteStop,
    RouteViolation,
    UnassignedOrder,
)
from apps.proof_of_delivery.models import ProofOfDelivery
from apps.reports.models import ReportExport
from apps.tracking.models import DriverEvent, VehicleLocation

if settings.ADMIN_MFA_REQUIRED:
    admin.site.__class__ = OTPAdminSite


@admin.register(User)
class LogisticsUserAdmin(UserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "is_staff", "is_active")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "locale")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "full_name", "password1", "password2")}),
    )
    search_fields = ("email", "full_name")


class OrganizationListAdmin(admin.ModelAdmin):
    list_display = ("id", "__str__", "organization", "created_at")
    list_filter = ("organization",)
    raw_id_fields = ()


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "status")
    search_fields = ("name", "slug")


@admin.register(OrganizationMembership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "active")
    list_filter = ("organization", "role", "active")
    filter_horizontal = ("depots",)


@admin.register(Depot)
class DepotAdmin(GISModelAdmin):
    list_display = ("code", "name", "organization", "active")
    list_filter = ("organization", "active")


@admin.register(Address)
class AddressAdmin(GISModelAdmin):
    list_display = ("line1", "city", "country_code", "organization", "geocode_status")
    list_filter = ("organization", "geocode_status", "country_code")
    search_fields = ("line1", "formatted_address", "city")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("external_ref", "organization", "service_date", "depot", "status", "priority")
    list_filter = ("organization", "service_date", "status", "depot")
    search_fields = ("external_ref", "customer__name")
    raw_id_fields = ("customer", "pickup_address", "delivery_address", "assigned_route_stop")


@admin.register(Vehicle)
class VehicleAdmin(GISModelAdmin):
    list_display = ("code", "plate_number", "depot", "vehicle_type", "status", "active")
    list_filter = ("organization", "depot", "status", "active")


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ("employee_code", "full_name", "depot", "status")
    list_filter = ("organization", "depot", "status")


@admin.register(PlanningRun)
class PlanningRunAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "depot", "service_date", "status", "progress_percent")
    list_filter = ("organization", "service_date", "status")
    readonly_fields = ("input_snapshot_json", "metrics_json")


@admin.register(RoutePlan)
class RoutePlanAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "service_date", "plan_version", "status", "dispatched_at")
    list_filter = ("organization", "service_date", "status")


@admin.register(Route)
class RouteAdmin(GISModelAdmin):
    list_display = ("id", "route_plan", "vehicle", "driver", "status", "total_distance_m")
    list_filter = ("route_plan__organization", "status")


@admin.register(RouteStop)
class RouteStopAdmin(GISModelAdmin):
    list_display = ("id", "route", "sequence", "stop_type", "order", "status")
    list_filter = ("route__route_plan__organization", "status", "stop_type")


for model in [
    RestrictedZone,
    Customer,
    GeocodeCache,
    VehicleAvailability,
    DriverShift,
    DriverDevice,
    ImportJob,
    ImportRowError,
    OrderItem,
    OrderTimeWindow,
    PlanningProfile,
    PlanningRunOrder,
    PlanningRunVehicle,
    DistanceMatrix,
    UnassignedOrder,
    RouteViolation,
    DriverEvent,
    VehicleLocation,
    ProofOfDelivery,
    OperationException,
    ReportExport,
    Notification,
    WebhookSubscription,
    WebhookDelivery,
    AuditLog,
    IdempotencyRecord,
]:
    admin.site.register(model)

admin.site.site_header = "Logistics Route Optimization"
admin.site.site_title = "Logistics Admin"
