from django.conf import settings
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    request = context.get("request")
    detail = response.data
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail["detail"])
        field_errors = {}
    else:
        message = "Validation failed" if response.status_code == 400 else "Request failed"
        field_errors = detail if isinstance(detail, dict) else {"non_field_errors": detail}
    response.data = {
        "code": getattr(exc, "default_code", "error"),
        "message": message,
        "field_errors": field_errors,
        "request_id": getattr(request, "request_id", None),
    }
    return response


class OrganizationPageNumberPagination(PageNumberPagination):
    """Page-number pagination with a bounded, client-controllable page size.

    The cap matters as much as the default: without it any caller could ask for
    the whole table in one response by passing ``?page_size=100000``, which
    defeats the point of paginating.
    """

    page_size_query_param = "page_size"
    max_page_size = 200

    def paginate_queryset(self, queryset, request, view=None):
        # Pagination over an unordered queryset is a correctness bug, not a
        # warning: without a total order the database may return rows in a
        # different sequence for each page, so a client walking pages 1..N sees
        # duplicates and misses rows. Several models here have no Meta.ordering,
        # so the tie-break is applied here rather than per ViewSet.
        if queryset.ordered:
            return super().paginate_queryset(queryset, request, view=view)
        return super().paginate_queryset(
            queryset.order_by(*(queryset.model._meta.pk.name,)), request, view=view
        )

    def get_paginated_response(self, data):
        # Must be a Response, not a bare dict: ListModelMixin returns this value
        # straight into finalize_response, which asserts on HttpResponseBase.
        return Response(
            {
                "count": self.page.paginator.count,
                "page": self.page.number,
                "page_size": self.get_page_size(self.request),
                "total_pages": self.page.paginator.num_pages,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "results": data,
            }
        )

    def get_page_size(self, request):
        raw = request.query_params.get(self.page_size_query_param)
        if not raw:
            return settings.API_PAGE_SIZE
        try:
            requested = int(raw)
        except (TypeError, ValueError):
            return settings.API_PAGE_SIZE
        if requested < 1:
            return settings.API_PAGE_SIZE
        return min(requested, self.max_page_size)

