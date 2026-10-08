from django.db.models import Q

DEPOT_LOOKUPS = {
    "depots.depot": "pk",
    "fleet.vehicle": "depot_id",
    "drivers.driver": "depot_id",
    "drivers.drivershift": "driver__depot_id",
    "orders.order": "depot_id",
    "orders.importjob": "depot_id",
    "planning.planningrun": "depot_id",
    "planning.routeplan": "depot_id",
    "planning.route": "route_plan__depot_id",
    "planning.routestop": "route__route_plan__depot_id",
    "proof_of_delivery.proofofdelivery": "route_stop__route__route_plan__depot_id",
}


def membership_depot_ids(request):
    """Depot primary keys the caller may act on, or ``None`` for no restriction.

    The distinction between "no membership" and "membership with no depots"
    carries the whole security model, so it must stay explicit. Returning
    ``None`` for both used to collapse them, and since every consumer reads
    ``None`` as *unrestricted*, a membership whose depots had been cleared
    silently gained read and write access to every depot in the organization —
    the opposite of the intended default.

    ``None`` therefore means only "no membership", which upstream permission
    checks reject. An empty list means a real membership that is scoped to
    nothing, and is honoured as such: the caller sees no rows and can validate
    no depot.
    """
    membership = getattr(request, "membership", None)
    if not membership:
        return None
    return list(membership.depots.values_list("pk", flat=True))


def scope_queryset_to_membership(queryset, request):
    depot_ids = membership_depot_ids(request)
    if depot_ids is None:
        return queryset
    model_label = queryset.model._meta.label_lower
    if model_label == "exceptions.operationexception":
        if not depot_ids:
            return queryset.none()
        return queryset.filter(
            Q(order__depot_id__in=depot_ids)
            | Q(route__route_plan__depot_id__in=depot_ids)
            | Q(route_stop__route__route_plan__depot_id__in=depot_ids)
        ).distinct()
    lookup = DEPOT_LOOKUPS.get(model_label)
    if lookup:
        if not depot_ids:
            return queryset.none()
        return queryset.filter(**{f"{lookup}__in": depot_ids})
    # A model with no depot relationship cannot be narrowed by depot, so it stays
    # bounded by organization scoping alone.
    return queryset


def scope_report_exports_to_membership(queryset, request):
    depot_ids = membership_depot_ids(request)
    if depot_ids is None:
        return queryset
    if not depot_ids:
        return queryset.none()
    scope = Q()
    for depot_id in depot_ids:
        scope |= Q(filters_json__depot_id=depot_id) | Q(filters_json__depot_id=str(depot_id))
    return queryset.filter(scope)
