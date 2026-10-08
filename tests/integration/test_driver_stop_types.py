"""Every stop type must render, including stops with no order attached.

Depot start/end and break stops are real ``RouteStop`` rows whose ``order`` is
NULL. Driver stop pages previously read ``stop.order.delivery_address`` directly,
so opening a depot stop raised ``UndefinedError`` and returned a 500 — an
unhandled crash in the middle of the driver's route timeline.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.test import Client
from django.utils import timezone

from apps.depots.models import Depot
from apps.drivers.models import Driver
from apps.fleet.models import Vehicle
from apps.organizations.models import Organization, OrganizationMembership
from apps.planning.models import PlanningProfile, PlanningRun, Route, RoutePlan, RouteStop

pytestmark = [pytest.mark.integration]


@pytest.fixture
def driver_route_with_mixed_stops(db):
    user_model = get_user_model()
    organization = Organization.objects.create(name="Stop Type Org", slug="stop-type-org")
    depot = Depot.objects.create(
        organization=organization,
        name="Stop Type Depot",
        code="ST",
        address_text="Depot Street 1",
        location=Point(106.8272, -6.2146, srid=4326),
    )
    user = user_model.objects.create_user(email="stops@example.com", password="demo123")
    OrganizationMembership.objects.create(
        organization=organization,
        user=user,
        role=OrganizationMembership.Role.DRIVER,
    )
    driver = Driver.objects.create(
        organization=organization,
        user=user,
        depot=depot,
        employee_code="ST-1",
        full_name="Stop Driver",
    )
    vehicle = Vehicle.objects.create(
        organization=organization,
        depot=depot,
        code="ST-VAN",
        plate_number="B 1001 ST",
        vehicle_type="VAN",
        start_location=Point(106.8272, -6.2146, srid=4326),
        end_location=Point(106.8272, -6.2146, srid=4326),
    )
    profile = PlanningProfile.objects.create(organization=organization, name="Stop Profile")
    run = PlanningRun.objects.create(
        organization=organization,
        depot=depot,
        service_date=timezone.localdate(),
        profile=profile,
        requested_by=user,
    )
    plan = RoutePlan.objects.create(
        organization=organization,
        planning_run=run,
        service_date=timezone.localdate(),
        depot=depot,
        created_by=user,
    )
    now = timezone.now()
    route = Route.objects.create(
        route_plan=plan,
        vehicle=vehicle,
        driver=driver,
        sequence=1,
        status=Route.Status.IN_PROGRESS,
        start_at_planned=now,
        end_at_planned=now,
    )
    depot_stop = RouteStop.objects.create(
        route=route,
        stop_type=RouteStop.StopType.DEPOT_START,
        sequence=1,
        location=Point(106.8272, -6.2146, srid=4326),
        planned_arrival_at=now,
        planned_departure_at=now,
        status=RouteStop.Status.PENDING,
    )
    break_stop = RouteStop.objects.create(
        route=route,
        stop_type=RouteStop.StopType.BREAK,
        sequence=2,
        location=Point(106.83, -6.21, srid=4326),
        planned_arrival_at=now,
        planned_departure_at=now,
        status=RouteStop.Status.PENDING,
    )
    return {"user": user, "route": route, "stops": [depot_stop, break_stop]}


@pytest.mark.django_db(transaction=True)
def test_depot_and_break_stops_render_without_a_delivery(driver_route_with_mixed_stops):
    client = Client()
    client.login(username="stops@example.com", password="demo123")

    for stop in driver_route_with_mixed_stops["stops"]:
        response = client.get(f"/driver/stop/{stop.id}/")
        assert response.status_code == 200, (stop.stop_type, response.status_code)
        # The page must state what the stop actually is rather than rendering a
        # broken delivery block.
        assert stop.get_stop_type_display() in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_driver_route_page_lists_every_stop(driver_route_with_mixed_stops):
    client = Client()
    client.login(username="stops@example.com", password="demo123")
    route = driver_route_with_mixed_stops["route"]

    response = client.get(f"/driver/route/{route.id}/")
    assert response.status_code == 200
