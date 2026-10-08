"""Bearer-token access for driver endpoints.

DRF authenticates inside the view, which is later than Django middleware. The
organization attached to the request therefore has to be resolved a second time
once the token has established the user, otherwise every JWT-protected endpoint
rejects its own caller with 403.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from rest_framework.test import APIClient

from apps.depots.models import Depot
from apps.drivers.models import Driver
from apps.organizations.models import Organization, OrganizationMembership

pytestmark = [pytest.mark.integration]


@pytest.fixture
def driver_account(db):
    user_model = get_user_model()
    organization = Organization.objects.create(name="Token Test", slug="token-test")
    depot = Depot.objects.create(
        organization=organization,
        name="Token Depot",
        code="TK",
        address_text="Test",
        location=Point(106.8272, -6.2146, srid=4326),
    )
    user = user_model.objects.create_user(email="token-driver@example.com", password="demo123")
    OrganizationMembership.objects.create(
        organization=organization,
        user=user,
        role=OrganizationMembership.Role.DRIVER,
    )
    Driver.objects.create(
        organization=organization,
        user=user,
        depot=depot,
        employee_code="TK-1",
        full_name="Token Driver",
    )
    return {"user": user, "organization": organization, "driver": user.driver_profile}


@pytest.mark.django_db(transaction=True)
def test_driver_token_grants_access_to_own_driver_endpoints(driver_account):
    client = APIClient()
    token_response = client.post(
        "/api/v1/auth/token/",
        {"email": "token-driver@example.com", "password": "demo123"},
        format="json",
    )
    assert token_response.status_code == 200, token_response.content

    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_response.json()['access']}")

    # The driver has no route yet, so the payload is empty — what matters is
    # that the request is authorised rather than rejected with 403.
    routes = client.get("/api/v1/driver/routes/today/")
    assert routes.status_code == 200, routes.content
    assert routes.json() == []

    live = client.get("/api/v1/live/routes/")
    assert live.status_code == 200, live.content


@pytest.mark.django_db(transaction=True)
def test_missing_token_is_still_rejected(driver_account):
    client = APIClient()
    response = client.get("/api/v1/driver/routes/today/")
    assert response.status_code in {401, 403}
