from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.gis.geos import Point, Polygon
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.customers.models import Address, Customer
from apps.depots.models import Depot, RestrictedZone
from apps.drivers.models import Driver, DriverShift
from apps.exceptions.models import OperationException
from apps.fleet.models import Vehicle
from apps.orders.models import Order
from apps.organizations.models import Organization, OrganizationMembership
from apps.planning.models import PlanningProfile

from ._demo_history import build_operational_history

# The single credential every demo account shares. Kept as a constant so the
# README, the command output and the seeding code cannot drift apart.
DEMO_PASSWORD = "demo123"

# Accounts from earlier revisions of this seeder. They are deactivated if
# present so a database seeded before the rename does not keep a working login
# that is no longer documented anywhere.
RETIRED_DEMO_EMAILS = ["dispatcher@example.com"]


class Command(BaseCommand):
    help = "Create an idempotent, operationally useful demo organization."

    @transaction.atomic
    def handle(self, *args, **options):
        zone = ZoneInfo("Asia/Jakarta")
        today = timezone.localdate(timezone=zone)
        organization, _ = Organization.objects.update_or_create(
            slug="nusa-logistics",
            defaults={
                "name": "Nusa Logistics",
                "timezone": "Asia/Jakarta",
                "default_currency": "IDR",
                "status": Organization.Status.ACTIVE,
            },
        )
        depot, _ = Depot.objects.update_or_create(
            organization=organization,
            code="JKT-01",
            defaults={
                "name": "Jakarta Central Depot",
                "address_text": "Jl. Gatot Subroto, Jakarta",
                "location": Point(106.8272, -6.2146, srid=4326),
                "service_area": Polygon(
                    (
                        (106.70, -6.38),
                        (107.00, -6.38),
                        (107.00, -6.08),
                        (106.70, -6.08),
                        (106.70, -6.38),
                    ),
                    srid=4326,
                ),
                "timezone": "Asia/Jakarta",
                "active": True,
            },
        )
        RestrictedZone.objects.update_or_create(
            organization=organization,
            name="Old Town Heavy Vehicle Restriction",
            defaults={
                "polygon": Polygon(
                    (
                        (106.804, -6.142),
                        (106.818, -6.142),
                        (106.818, -6.128),
                        (106.804, -6.128),
                        (106.804, -6.142),
                    ),
                    srid=4326,
                ),
                "prohibited_vehicle_types": ["TRUCK"],
            },
        )
        users = {}
        for email, name, role in [
            ("demo@example.com", "Citra Dispatcher", OrganizationMembership.Role.DISPATCHER),
            ("admin@example.com", "Alya Administrator", OrganizationMembership.Role.ADMIN),
            ("operations@example.com", "Bima Operations", OrganizationMembership.Role.OPERATIONS_MANAGER),
            ("driver1@example.com", "Danu Driver", OrganizationMembership.Role.DRIVER),
            ("driver2@example.com", "Eka Driver", OrganizationMembership.Role.DRIVER),
            ("driver3@example.com", "Fajar Driver", OrganizationMembership.Role.DRIVER),
        ]:
            user, created = User.objects.get_or_create(
                email=email, defaults={"full_name": name, "is_staff": role == OrganizationMembership.Role.ADMIN}
            )
            # Always reset the password rather than only on creation: an existing
            # database may hold this account with a stale password from an earlier
            # seed, and the documented demo credentials have to keep working.
            user.full_name = name
            user.is_staff = role == OrganizationMembership.Role.ADMIN
            user.set_password(DEMO_PASSWORD)
            user.save()
            membership, _ = OrganizationMembership.objects.update_or_create(
                organization=organization,
                user=user,
                defaults={"role": role, "active": True},
            )
            membership.depots.add(depot)
            users[email] = user

        # Retire credentials this command used to create. Deleting the user would
        # cascade into the operational history that references it, so the
        # membership is deactivated instead and the login stops working.
        OrganizationMembership.objects.filter(
            organization=organization, user__email__in=RETIRED_DEMO_EMAILS
        ).update(active=False)
        vehicles = []
        for index, vehicle_type in enumerate(["VAN", "VAN", "TRUCK"], start=1):
            vehicle, _ = Vehicle.objects.update_or_create(
                organization=organization,
                code=f"JKT-V{index:02}",
                defaults={
                    "depot": depot,
                    "plate_number": f"B 10{index:02} LRO",
                    "vehicle_type": vehicle_type,
                    "capacity_weight_kg": Decimal("900") if vehicle_type == "VAN" else Decimal("3500"),
                    "capacity_volume_m3": Decimal("8") if vehicle_type == "VAN" else Decimal("22"),
                    "capacity_package_count": 80 if vehicle_type == "VAN" else 240,
                    "max_stops": 35,
                    "max_route_duration_seconds": 36000,
                    "skills_json": ["COLD_CHAIN"] if index == 2 else ["STANDARD"],
                    "start_location": depot.location,
                    "end_location": depot.location,
                    "fixed_cost": Decimal("150000"),
                    "cost_per_km": Decimal("4500"),
                    "cost_per_hour": Decimal("45000"),
                },
            )
            vehicles.append(vehicle)
        drivers = []
        for index, email in enumerate(
            ["driver1@example.com", "driver2@example.com", "driver3@example.com"], start=1
        ):
            driver, _ = Driver.objects.update_or_create(
                organization=organization,
                employee_code=f"DRV-{index:03}",
                defaults={
                    "user": users[email],
                    "depot": depot,
                    "full_name": users[email].full_name,
                    "phone_encrypted": f"+62812000000{index}",
                    "skills_json": ["COLD_CHAIN", "STANDARD"] if index == 2 else ["STANDARD"],
                },
            )
            # One shift per service day: the previous day needs a shift as well,
            # otherwise the planner falls back to a default window and the
            # generated history would not reflect the real operating hours.
            for shift_date in (today - timezone.timedelta(days=1), today):
                DriverShift.objects.update_or_create(
                    driver=driver,
                    shift_date=shift_date,
                    start_at=timezone.make_aware(datetime.combine(shift_date, time(7, 0)), zone),
                    defaults={
                        "end_at": timezone.make_aware(datetime.combine(shift_date, time(17, 0)), zone),
                        "start_location": depot.location,
                        "end_location": depot.location,
                        "break_rules_json": [{"earliest_s": 14400, "duration_s": 1800}],
                    },
                )
            drivers.append(driver)
        addresses = [
            ("Kebayoran", "Jl. Senopati No. 12", 106.8080, -6.2332),
            ("Menteng", "Jl. HOS Cokroaminoto No. 8", 106.8295, -6.1954),
            ("Kuningan", "Jl. HR Rasuna Said Kav. 4", 106.8353, -6.2201),
            ("Kelapa Gading", "Jl. Boulevard Raya No. 21", 106.9022, -6.1588),
            ("Cilandak", "Jl. TB Simatupang No. 17", 106.8015, -6.2898),
            ("Sunter", "Jl. Danau Sunter Utara No. 5", 106.8704, -6.1406),
            ("Palmerah", "Jl. Palmerah Barat No. 33", 106.7972, -6.2047),
            ("Tebet", "Jl. Tebet Raya No. 14", 106.8524, -6.2298),
            ("Kemang", "Jl. Kemang Raya No. 44", 106.8166, -6.2607),
            ("Pademangan", "Jl. Gunung Sahari No. 9", 106.8417, -6.1446),
            ("Pondok Indah", "Jl. Metro Pondok Indah No. 3", 106.7836, -6.2650),
            ("Rawamangun", "Jl. Pemuda No. 18", 106.8916, -6.1934),
        ]
        # Orders exist for the previous service day as well, so the completed
        # history, reports and audit views have real rows to summarise.
        service_days = [today - timezone.timedelta(days=1), today]
        for index, (name, line, longitude, latitude) in enumerate(addresses, start=1):
            customer, _ = Customer.objects.update_or_create(
                organization=organization,
                external_ref=f"CUST-{index:03}",
                defaults={"name": f"{name} Market", "phone_encrypted": f"+62215550{index:03}"},
            )
            address, _ = Address.objects.update_or_create(
                organization=organization,
                customer=customer,
                label="Main",
                defaults={
                    "line1": line,
                    "city": "Jakarta",
                    "region": "DKI Jakarta",
                    "postal_code": f"12{index:03}",
                    "country_code": "ID",
                    "formatted_address": f"{line}, Jakarta, Indonesia",
                    "location": Point(longitude, latitude, srid=4326),
                    "geocode_status": Address.GeocodeStatus.VALID,
                    "geocode_provider": "seed",
                    "geocode_confidence": Decimal("1.0"),
                },
            )
            for service_date in service_days:
                day_offset = (today - service_date).days
                window_start = timezone.make_aware(
                    datetime.combine(service_date, time(8, 0)), zone
                ) + timezone.timedelta(minutes=(index % 4) * 90)
                Order.objects.update_or_create(
                    organization=organization,
                    external_ref=f"ORD-{service_date:%Y%m%d}-{index:03}",
                    defaults={
                        "depot": depot,
                        "customer": customer,
                        "status": Order.Status.READY,
                        "priority": 1 + index % 5,
                        "delivery_address": address,
                        "service_date": service_date,
                        "time_window_start": window_start,
                        "time_window_end": window_start + timezone.timedelta(minutes=180),
                        "service_duration_seconds": 600,
                        "demand_weight_kg": Decimal(35 + index * 4),
                        "demand_volume_m3": Decimal("0.35"),
                        "package_count": 2 + index + day_offset,
                        "required_skills_json": ["COLD_CHAIN"] if index in {3, 8} else ["STANDARD"],
                        "created_by": users["demo@example.com"],
                    },
                )
        profile, _ = PlanningProfile.objects.update_or_create(
            organization=organization,
            name="Balanced daily delivery",
            version=1,
            defaults={
                "objective_weights_json": {
                    "distance": 1,
                    "duration": 1,
                    "unassigned": 1000000,
                    "vehicle_fixed": 150000,
                    "workload_imbalance": 250,
                    # Keeps the currency-based weights on the same scale as the
                    # metre-based travel cost. See apps/optimization/services.py.
                    "idr_per_objective_unit": 100,
                },
                "default_constraints_json": {"hard_time_windows": True, "allow_unassigned": True},
            },
        )
        if not OperationException.objects.filter(
            organization=organization, type="ADDRESS_REVIEW", status=OperationException.Status.OPEN
        ).exists():
            OperationException.objects.create(
                organization=organization,
                type="ADDRESS_REVIEW",
                severity=OperationException.Severity.MEDIUM,
                description="A newly imported address needs dispatcher confirmation.",
                reported_by=users["operations@example.com"],
                assigned_to=users["demo@example.com"],
            )

        # Planning, dispatch and field execution run through the real service
        # layer so the operational pages are not empty and the demo data cannot
        # drift away from the behaviour it is meant to demonstrate.
        history = build_operational_history(
            organization=organization,
            depot=depot,
            users=users,
            drivers=drivers,
            vehicles=vehicles,
            today=today,
            zone=zone,
        )
        resolved = _resolve_demo_exception(organization=organization, users=users)
        self.stdout.write(
            f"Planning history: yesterday={_plan_label(history.get('yesterday'))}, "
            f"today={_plan_label(history.get('today'))}"
        )
        if resolved:
            self.stdout.write(f"Resolved {resolved} driver-reported exception(s) for the demo.")
        self.stdout.write(self.style.SUCCESS("Demo data is ready. Login: demo@example.com / demo123"))


def _plan_label(plan):
    if plan is None:
        return "none"
    return f"{plan.routes.count()} route(s) v{plan.plan_version} [{plan.status}]"


def _resolve_demo_exception(*, organization, users):
    """Close out the previous day's driver-reported exception.

    The queue should show both halves of the triage workflow: yesterday's failed
    delivery, already investigated and closed, alongside today's failure, which
    is still open and awaiting attention. Only exceptions raised before today
    are resolved, so an open item always remains for the operations view to act
    on.
    """
    zone = ZoneInfo("Asia/Jakarta")
    today = timezone.localdate(timezone=zone)
    exception = (
        OperationException.objects.filter(
            organization=organization,
            type="CUSTOMER_UNAVAILABLE",
            status=OperationException.Status.OPEN,
            created_at__date__lt=today,
        )
        .order_by("pk")
        .first()
    )
    if exception is None:
        return 0
    exception.status = OperationException.Status.RESOLVED
    exception.assigned_to = users["operations@example.com"]
    exception.resolution = "Customer confirmed the delivery can be reattempted on the next service day."
    exception.resolved_at = exception.created_at + timedelta(hours=3)
    exception.version += 1
    exception.save(
        update_fields=["status", "assigned_to", "resolution", "resolved_at", "version", "updated_at"]
    )
    return 1
