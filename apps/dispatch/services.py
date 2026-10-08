from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.services import record_audit
from apps.common.state_machine import (
    PLAN_TRANSITIONS,
    ROUTE_TRANSITIONS,
    RUN_TRANSITIONS,
    assert_transition,
)
from apps.integrations.models import Notification
from apps.integrations.services import emit_webhook_event
from apps.orders.models import Order
from apps.planning.models import PlanningRun, Route, RoutePlan, RouteViolation
from apps.planning.services import validate_plan


@transaction.atomic
def dispatch_plan(plan_id, actor, request=None, expected_version=None):
    plan = RoutePlan.objects.select_for_update().select_related("organization", "planning_run").get(pk=plan_id)
    if expected_version is not None and plan.version != int(expected_version):
        raise ValueError(
            f"Version conflict: expected {expected_version}, current version is {plan.version}"
        )
    assert_transition(
        plan.status,
        RoutePlan.Status.DISPATCHED,
        PLAN_TRANSITIONS,
        entity="Route plan",
    )
    validate_plan(plan)
    if plan.violations.filter(severity=RouteViolation.Severity.ERROR, acknowledged_at__isnull=True).exists():
        raise ValueError("Route plan has blocking violations")
    routes = list(plan.routes.select_related("driver__user"))
    if not routes or any(route.driver_id is None for route in routes):
        raise ValueError("Every route must have an assigned driver")
    now = timezone.now()
    plan.status = RoutePlan.Status.DISPATCHED
    plan.dispatched_at = now
    plan.version += 1
    plan.save(update_fields=["status", "dispatched_at", "version", "updated_at"])
    run_status = PlanningRun.Status.DISPATCHED
    assert_transition(
        plan.planning_run.status,
        run_status,
        RUN_TRANSITIONS,
        entity="Planning run",
    )
    plan.planning_run.status = run_status
    plan.planning_run.version += 1
    plan.planning_run.save(update_fields=["status", "version", "updated_at"])
    for route in routes:
        assert_transition(
            route.status,
            Route.Status.DISPATCHED,
            ROUTE_TRANSITIONS,
            entity="Route",
        )
        route.status = Route.Status.DISPATCHED
        route.version += 1
        route.save(update_fields=["status", "version", "updated_at"])
        route.stops.filter(order__isnull=False).update(
            status="PENDING",
            version=F("version") + 1,
            updated_at=now,
        )
        Order.objects.filter(route_stops__route=route).update(
            status=Order.Status.DISPATCHED,
            version=F("version") + 1,
            updated_at=now,
        )
        if route.driver.user_id:
            Notification.objects.create(
                organization=plan.organization,
                recipient=route.driver.user,
                type="ROUTE_DISPATCHED",
                title="A route has been dispatched",
                message=f"Route {route.pk} is ready for {plan.service_date}.",
                payload_json={"route_id": route.pk, "route_plan_version": plan.plan_version},
            )
    record_audit(
        organization=plan.organization,
        actor=actor,
        action="route_plan.dispatched",
        instance=plan,
        after={"status": plan.status, "dispatched_at": now},
        request=request,
    )
    # Queued after the audit row and on commit, so a subscriber is never told
    # about a dispatch that a later rollback undoes.
    transaction.on_commit(
        lambda: emit_webhook_event(
            plan.organization,
            "plan.dispatched",
            {
                "plan_id": plan.pk,
                "plan_version": plan.plan_version,
                "service_date": plan.service_date.isoformat(),
                "depot_code": plan.depot.code if plan.depot_id else None,
                "route_ids": [route.pk for route in routes],
            },
        )
    )
    return plan
