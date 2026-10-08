"""Views must not crash for an authenticated account with no active membership.

Memberships are deactivated rather than deleted when someone leaves, and the
demo seeder retires old accounts the same way. Such a user can still
authenticate, so every application view has to handle the missing organization
instead of dereferencing ``None`` and returning a 500.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.organizations.models import Organization, OrganizationMembership

pytestmark = [pytest.mark.integration]


@pytest.fixture
def user_without_membership(db):
    user_model = get_user_model()
    organization = Organization.objects.create(name="Dormant Org", slug="dormant-org")
    user = user_model.objects.create_user(email="dormant@example.com", password="demo123")
    OrganizationMembership.objects.create(
        organization=organization,
        user=user,
        role=OrganizationMembership.Role.DISPATCHER,
        active=False,
    )
    return user


@pytest.mark.django_db(transaction=True)
def test_session_without_membership_is_redirected_not_broken(user_without_membership):
    client = Client()
    assert client.login(username="dormant@example.com", password="demo123") is True

    # Previously this raised AttributeError on ``None.timezone`` inside the
    # dashboard view, surfacing as a 500.
    response = client.get("/app/dashboard/")
    assert response.status_code == 302
    assert response["Location"] == "/no-organization/"

    page = client.get("/no-organization/")
    assert page.status_code == 403
    # The session must not be left half-open once the reason is explained.
    assert client.get("/app/dashboard/").status_code == 302


@pytest.mark.django_db(transaction=True)
def test_inactive_membership_cannot_browse_any_application_page(user_without_membership):
    client = Client()
    client.login(username="dormant@example.com", password="demo123")
    for path in ("/app/orders/", "/app/routes/", "/app/settings/", "/driver/today/"):
        response = client.get(path)
        assert response.status_code in {302, 403}, (path, response.status_code)
        assert response.status_code != 500, path
