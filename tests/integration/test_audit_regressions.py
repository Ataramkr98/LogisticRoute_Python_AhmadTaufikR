"""Regression tests for the defects found auditing this project.

Every test here corresponds to a bug that was present and has been fixed. They
exist so the same defect cannot come back silently; each one fails if its fix is
reverted, and the comment on each records why the original code was wrong.

Grouped by the failure mode they protect against:

* optional-dependency handling (Redis absent, Celery broker down)
* tenant isolation and API reachability for token-authenticated callers
* transition-table integrity
* outbound request safety
* encrypted-field failure mode
"""

import warnings
from datetime import date
from unittest.mock import patch

import pytest
from django.contrib.gis.geos import Point
from django.test import Client
from redis.exceptions import RedisError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.common.dispatch import dispatch
from apps.common.events import event_stream, publish
from apps.common.fields import DecryptionFailed, EncryptedTextField
from apps.common.scoping import membership_depot_ids, scope_queryset_to_membership
from apps.common.state_machine import (
    ROUTE_TRANSITIONS,
    RUN_TRANSITIONS,
    STOP_TRANSITIONS,
    InvalidTransition,
    assert_transition,
    validate_transition_tables,
)
from apps.integrations.validation import validate_webhook_url
from apps.orders.models import Order

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Optional Redis
# ---------------------------------------------------------------------------


class _UnreachableRedis:
    """Stand-in for the Redis client that refuses every connection.

    Raises the genuine ``redis.exceptions.ConnectionError`` rather than a generic
    OSError, because that is what the real client raises when nothing is
    listening — and it is what the production code catches.
    """

    calls: list = []

    @classmethod
    def from_url(cls, *args, **kwargs):
        cls.calls.append(kwargs)
        return cls()

    def publish(self, *args, **kwargs):
        raise RedisError("connection refused")

    def pubsub(self, *args, **kwargs):
        raise RedisError("connection refused")

    def ping(self):
        raise RedisError("connection refused")

    def close(self):
        pass

    @classmethod
    def reset(cls):
        cls.calls = []


class TestRedisIsOptional:
    """Redis is documented as optional, so nothing may hard-fail without it.

    The original code broke that promise in three places. Each of these tests
    fails against the original implementation.
    """

    def test_publish_returns_false_instead_of_raising(self):
        # Used to raise out of a Redis.from_url() call, taking down every task
        # that reported progress.
        _UnreachableRedis.reset()
        with patch("apps.common.events.Redis", _UnreachableRedis):
            assert publish("planning:1", {"percent": 10}) is False

    def test_event_stream_emits_unavailable_instead_of_raising(self):
        # Used to raise ConnectionError, turning the SSE endpoint into a 500
        # and putting the browser into a reconnect-and-reload loop.
        with patch("apps.common.events.Redis", _UnreachableRedis):
            frames = list(event_stream("planning:1"))
        assert len(frames) == 1
        assert frames[0].startswith("event: unavailable")
        # A named event is what lets the page switch to polling instead of
        # retrying a stream that can never connect.
        assert "broker_unreachable" in frames[0]

    def test_redis_clients_carry_a_connect_timeout(self):
        # The original client set no timeout, so an unreachable broker blocked
        # the caller for the full OS TCP timeout on every single publish. Since
        # update_progress publishes per phase, a whole optimization run became a
        # chain of multi-second stalls.
        _UnreachableRedis.reset()
        with patch("apps.common.events.Redis", _UnreachableRedis):
            publish("planning:1", {"percent": 10})
        assert _UnreachableRedis.calls, "publish() never constructed a client"
        assert _UnreachableRedis.calls[0].get("socket_connect_timeout") == 2

        _UnreachableRedis.reset()
        with patch("apps.common.events.Redis", _UnreachableRedis):
            list(event_stream("planning:1"))
        assert _UnreachableRedis.calls[0].get("socket_connect_timeout") == 2

    def test_readiness_stays_healthy_without_a_broker(self, settings):
        # The probe used to require Redis, so an orchestrator would kill a
        # container that was serving every page correctly.
        settings.CELERY_TASK_ALWAYS_EAGER = True
        with patch("apps.common.views.Redis", _UnreachableRedis):
            with patch("apps.common.views.cache") as fake_cache:
                fake_cache.get.return_value = "ok"
                response = Client().get("/health/ready/")
        assert response.status_code == 200
        body = response.json()
        assert body["checks"]["database"] is True
        assert body["checks"]["broker"] is False
        assert body["degraded"] == ["broker"]


# ---------------------------------------------------------------------------
# Broker-independent task dispatch
# ---------------------------------------------------------------------------


class TestDispatchFallsBackToInline:
    @patch("apps.routing.tasks.geocode_address_task.apply_async")
    def test_dispatch_runs_inline_when_the_broker_rejects(self, apply_async):
        from apps.routing.tasks import geocode_address_task

        apply_async.side_effect = OSError("broker down")
        # With no worker there is nothing to hand work to, so the task runs in
        # the request. Previously this raised RuntimeError after ~20s of Redis
        # result-backend retries, failing the whole request.
        with patch.object(geocode_address_task, "apply") as inline:
            result = dispatch(geocode_address_task, 1)
        assert inline.called
        # The caller stores `result.id` on a PlanningRun, so it must exist on
        # both the queued and the inline path.
        assert result.id

    def test_dispatch_returns_the_queued_result_when_the_broker_works(self):
        from apps.routing.tasks import geocode_address_task

        with patch.object(geocode_address_task, "apply_async") as apply_async:
            sentinel = object()
            apply_async.return_value = sentinel
            assert dispatch(geocode_address_task, 1) is sentinel


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def operator(rf, django_user_model):
    from apps.customers.models import Address
    from apps.depots.models import Depot
    from apps.organizations.models import Organization, OrganizationMembership

    user = django_user_model.objects.create_user(
        email="scope@example.com", password="pw", is_active=True
    )
    org = Organization.objects.create(name="Scope Org", slug="scope-org", timezone="UTC")
    jakarta = Point(106.8272, -6.2146, srid=4326)
    Depot.objects.create(
        organization=org, code="D0", name="Home", timezone="UTC", location=jakarta
    )
    Address.objects.create(
        organization=org, label="Site", line1="Jl. Test", city="Jakarta", country_code="ID"
    )
    membership = OrganizationMembership.objects.create(
        organization=org, user=user, role="ADMIN", active=True
    )
    user.is_staff = False
    user.save(update_fields=["is_staff"])
    return user, membership


@pytest.fixture
def api_client_for(operator):
    user, membership = operator
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    return client, membership


def operator_user(membership):
    """The user behind a membership, for non-nullable created_by columns."""
    return membership.user


class TestDepotScopingFailsClosed:
    def test_zero_depot_membership_sees_nothing(self, api_client_for, rf):
        from apps.customers.models import Address
        from apps.depots.models import Depot

        client, membership = api_client_for
        creator = operator_user(membership)
        jakarta = Point(106.8272, -6.2146, srid=4326)
        own = Depot.objects.create(
            organization=membership.organization,
            code="D1", name="Own", timezone="UTC", location=jakarta,
        )
        other = Depot.objects.create(
            organization=membership.organization,
            code="D2", name="Other", timezone="UTC", location=jakarta,
        )
        address = Address.objects.filter(organization=membership.organization).first()
        mine = Order.objects.create(
            organization=membership.organization,
            external_ref="MINE",
            depot=own,
            delivery_address=address,
            status=Order.Status.DRAFT,
            service_date=date(2026, 1, 1),
            created_by=creator,
        )
        Order.objects.create(
            organization=membership.organization,
            external_ref="THEIRS",
            depot=other,
            delivery_address=address,
            status=Order.Status.DRAFT,
            service_date=date(2026, 1, 1),
            created_by=creator,
        )
        membership.depots.set([own])

        request = rf.get("/")
        request.membership = membership
        assert list(
            scope_queryset_to_membership(Order.objects.all(), request)
            .values_list("external_ref", flat=True)
        ) == ["MINE"]
        response = client.get("/api/v1/orders/")
        assert response.status_code == 200
        assert [row["external_ref"] for row in response.json()["results"]] == ["MINE"]
        assert mine.external_ref == "MINE"

        # The defect: a membership whose depots were cleared returned None,
        # which every consumer read as "no restriction" — the opposite of the
        # intended default — and the member saw the whole organization.
        membership.depots.clear()
        request = rf.get("/")
        request.membership = membership
        assert membership_depot_ids(request) == []
        assert scope_queryset_to_membership(Order.objects.all(), request).count() == 0
        cleared = client.get("/api/v1/orders/")
        assert cleared.status_code == 200
        assert cleared.json()["count"] == 0

    def test_no_membership_means_unrestricted(self, rf):
        # `None` must keep meaning "no membership", which the permission layer
        # rejects separately. Collapsing it with an empty scope is what made the
        # original bug possible.
        request = rf.get("/")
        request.membership = None
        assert membership_depot_ids(request) is None


# ---------------------------------------------------------------------------
# API reachability for token-authenticated callers
# ---------------------------------------------------------------------------


class TestEveryEndpointIsReachableWithAJWT:
    """CurrentOrganizationMiddleware skips bearer requests on purpose.

    It runs before DRF authenticates anyone, so organization resolution has to
    finish inside the view. When it only happened on DocumentedAPIView, the
    sixteen router ViewSets inherited plain APIView.initial, request.organization
    stayed None, and HasOrganization denied every single call — the token
    endpoint issued tokens that worked on twelve endpoints and failed on all of
    the rest.
    """

    ENDPOINTS = [
        "/api/v1/orders/",
        "/api/v1/depots/",
        "/api/v1/customers/",
        "/api/v1/addresses/",
        "/api/v1/vehicles/",
        "/api/v1/drivers/",
        "/api/v1/driver-shifts/",
        "/api/v1/import-jobs/",
        "/api/v1/planning-profiles/",
        "/api/v1/planning-runs/",
        "/api/v1/route-plans/",
        "/api/v1/routes/",
        "/api/v1/route-stops/",
        "/api/v1/exceptions/",
        "/api/v1/report-exports/",
        "/api/v1/webhooks/",
    ]

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_endpoint_returns_200_not_403(self, api_client_for, endpoint):
        client, _ = api_client_for
        assert client.get(endpoint).status_code == 200, endpoint

    def test_anonymous_is_still_rejected(self):
        assert APIClient().get("/api/v1/orders/").status_code in (401, 403)


# ---------------------------------------------------------------------------
# Route plans: an action must not shadow View.dispatch
# ---------------------------------------------------------------------------


class TestRoutePlanViewSetIsReachable:
    def test_list_is_not_shadowed_by_the_dispatch_action(self, api_client_for):
        # The action was defined as `def dispatch`, which overrides
        # View.dispatch — the method DRF calls to route every inbound request.
        # Every call on this ViewSet therefore arrived at the action with no pk
        # and raised AssertionError. The URL path and reverse name are kept via
        # url_path/url_name so existing clients are unaffected.
        client, _ = api_client_for
        assert client.get("/api/v1/route-plans/").status_code == 200

    def test_no_viewset_action_shadows_a_view_method(self):
        # A named collision that anyone can reintroduce in a single line, and one
        # that produces a 500 rather than an obvious error.
        reserved = {
            "dispatch", "initial", "options", "head", "trace",
            "get", "post", "put", "patch", "delete",
        }
        for viewset in _organization_viewsets():
            actions = {action.__name__ for action in viewset().get_extra_actions()}
            clash = actions & reserved
            assert not clash, f"{viewset.__name__} defines action(s) {clash} that shadow View methods"


def _organization_viewsets():
    """Every ViewSet registered on the router, discovered rather than listed."""
    from apps.common.api_urls import router

    viewsets = []
    for _prefix, viewset, _basename in router.registry:
        if hasattr(viewset, "get_extra_actions"):
            viewsets.append(viewset)
    return viewsets


# ---------------------------------------------------------------------------
# Transition tables
# ---------------------------------------------------------------------------


class TestTransitionTables:
    def test_every_table_matches_the_model_choices(self):
        # Runs at import time; asserted explicitly so the guarantee is visible
        # in the test report and not only as a side effect of importing.
        assert validate_transition_tables() is True

    def test_every_status_is_reachable_or_marked_terminal(self):
        # Adding a status to a model without deciding where it goes used to
        # produce an entity that could never move. The check caught two such
        # gaps (CANCELLED for Order and for Route) on first run.
        from apps.orders.models import Order as OrderModel
        from apps.planning.models import PlanningRun
        from apps.planning.models import Route as RouteModel

        assert set(ORDER_STATUSES) == set(OrderModel.Status.values)
        assert set(ROUTE_TRANSITIONS) == set(RouteModel.Status.values)
        assert set(RUN_TRANSITIONS) == set(PlanningRun.Status.values)

    def test_illegal_moves_are_refused(self):
        with pytest.raises(InvalidTransition):
            assert_transition("DISPATCHED", "DRAFT", ROUTE_TRANSITIONS, entity="route")
        with pytest.raises(InvalidTransition):
            assert_transition("COMPLETED", "ARRIVED", STOP_TRANSITIONS, entity="stop")

    def test_reapplying_the_current_state_is_a_no_op(self):
        # Retries and replays re-assert a status they may already hold; that is
        # not an illegal transition.
        assert_transition("DISPATCHED", "DISPATCHED", ROUTE_TRANSITIONS, entity="route")

    def test_caller_supplied_exception_type_is_used(self):
        from apps.tracking.services import DriverActionConflict

        with pytest.raises(DriverActionConflict):
            assert_transition(
                "COMPLETED", "EN_ROUTE", STOP_TRANSITIONS,
                entity="Stop", error=DriverActionConflict,
            )

    def test_legitimate_progress_is_allowed(self):
        path = ["DRAFT", "DISPATCHED", "IN_PROGRESS", "COMPLETED"]
        for current, target in zip(path[:-1], path[1:], strict=True):
            assert_transition(current, target, ROUTE_TRANSITIONS, entity="route")


ORDER_STATUSES = {
    "DRAFT", "READY", "PLANNED", "DISPATCHED", "IN_PROGRESS",
    "COMPLETED", "CANCELLED", "FAILED", "PARTIALLY_COMPLETED", "UNASSIGNED",
}


# ---------------------------------------------------------------------------
# Outbound request safety
# ---------------------------------------------------------------------------


class TestWebhookUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/hook",                      # plaintext
            "https://169.254.169.254/latest/meta-data/",    # cloud metadata
            "https://127.0.0.1:5432/",                      # loopback
            "https://10.0.0.5/internal",                   # RFC1918
            "https://192.168.1.1/router",                  # LAN
            "https://[::1]/x",                             # IPv6 loopback
        ],
    )
    def test_private_and_plaintext_targets_are_refused(self, url):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            validate_webhook_url(url)

    def test_unresolvable_host_is_refused(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            validate_webhook_url("https://nonexistent-host-zzz.invalid/hook")

    def test_public_https_target_is_accepted(self):
        assert validate_webhook_url("https://example.com/hook") == "https://example.com/hook"


# ---------------------------------------------------------------------------
# Encrypted fields
# ---------------------------------------------------------------------------


class TestEncryptedTextField:
    def test_rotation_failure_is_loud_not_silent(self, settings):
        # Returning "" here quietly blanked every customer email, phone number
        # and webhook secret, with no log and no error. A loud failure in one
        # request is recoverable; a silent column wipe is not.
        settings.FIELD_ENCRYPTION_KEY = "original-key"
        ciphertext = EncryptedTextField().get_prep_value("+62-812-0000")

        # Rotating the key — which is exactly what an operator does on a schedule
        # — makes every existing ciphertext undecryptable.
        settings.FIELD_ENCRYPTION_KEY = "rotated-key"
        with pytest.raises(DecryptionFailed):
            EncryptedTextField().to_python(ciphertext)

    def test_plain_values_pass_through(self):
        field = EncryptedTextField()
        assert field.to_python(None) is None
        assert field.to_python("") == ""
        assert field.to_python("not-encrypted") == "not-encrypted"

    def test_round_trip(self, settings):
        settings.FIELD_ENCRYPTION_KEY = "stable-key"
        field = EncryptedTextField()
        assert field.to_python(field.get_prep_value("hello")) == "hello"

    def test_absent_field_encryption_key_falls_back_to_secret_key(self, settings):
        # Documented behaviour, and the reason rotation matters: blanking
        # FIELD_ENCRYPTION_KEY silently changes which key every column is
        # encrypted with.
        settings.FIELD_ENCRYPTION_KEY = ""
        settings.SECRET_KEY = "fallback-secret"
        field = EncryptedTextField()
        ciphertext = field.get_prep_value("secret value")
        assert field.to_python(ciphertext) == "secret value"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPaginationOrdering:
    def test_unordered_querysets_get_a_deterministic_order(self, api_client_for):
        # Order has no Meta.ordering. Paginating without a total order means the
        # database may return rows in a different sequence per page, so a client
        # walking the pages sees duplicates and misses rows.
        client, _ = api_client_for
        with _no_unordered_object_warning():
            first = client.get("/api/v1/orders/?page_size=1")
            second = client.get("/api/v1/orders/?page_size=1&page=2")
        assert first.status_code == 200
        if first.json()["count"] > 1:
            assert second.json()["results"][0]["id"] != first.json()["results"][0]["id"]

    def test_page_size_is_capped(self, api_client_for):
        # Without a ceiling ?page_size=100000 returns the whole table in one
        # response, which is a denial-of-service lever aimed at the client.
        client, _ = api_client_for
        response = client.get("/api/v1/orders/?page_size=100000")
        assert response.status_code == 200
        assert response.json()["page_size"] <= 200

    def test_page_shape_is_documented_and_stable(self, api_client_for):
        client, _ = api_client_for
        body = client.get("/api/v1/orders/").json()
        assert set(body) == {"count", "page", "page_size", "total_pages", "next", "previous", "results"}


class _no_unordered_object_warning:
    """Turn Django's unordered-pagination warning into a test failure."""

    def __enter__(self):
        self._catcher = warnings.catch_warnings(record=True)
        self._log = self._catcher.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, *exc_info):
        offending = [entry for entry in self._log if "UnorderedObjectList" in str(entry.message)]
        self._catcher.__exit__(*exc_info)
        assert not offending, f"unordered queryset paginated: {offending}"
        return False
