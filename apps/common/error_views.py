"""Error page handlers.

Django falls back to its own plain-text 404/500 pages unless a project supplies
``handler404``/``handler500``, so before this existed a mistyped URL or an
uncaught exception rendered a bare white page with none of the application's
styling. These handlers render the branded pages in ``templates_jinja2/errors/``
instead.

Every handler works from ``request`` alone. That is deliberate: a 500 means the
request pipeline already failed, so anything derived from the database, the
session or the organization context processor is a liability rather than an
asset. The templates therefore never dereference ``request.organization``.
"""

import logging

from django.http import HttpResponse
from django.shortcuts import render

logger = logging.getLogger(__name__)

_STATUS = {
    400: ("Bad request", "The request could not be processed."),
    403: ("Permission denied", "You do not have access to this resource."),
    404: ("Page not found", "The page or record you requested does not exist."),
    500: ("Something went wrong", "The request failed before it could complete."),
}


def _render_error(request, status_code):
    """Render the branded error page for ``status_code``.

    Falls back to a minimal inline document if the Jinja2 page cannot be
    rendered. Raising inside an error handler would replace a readable 404 with
    an unreadable 500, which helps nobody.
    """
    headline, detail = _STATUS[status_code]
    user = getattr(request, "user", None)
    context = {
        "status_code": status_code,
        "headline": headline,
        "detail": detail,
        # Set by RequestIDMiddleware; absent only if middleware itself failed.
        "request_id": getattr(request, "request_id", "") or "",
        "request": request,
        # Resolved here rather than read as request.user in the template. A 500
        # can be raised from inside AuthenticationMiddleware, in which case
        # request.user does not exist and touching it in the template would
        # raise a second error inside the handler for the first one.
        "is_authenticated": bool(user and getattr(user, "is_authenticated", False)),
        "request_path": getattr(request, "path", ""),
    }
    try:
        return render(request, f"errors/{status_code}.html", context, status=status_code)
    except Exception:
        logger.exception("error_page_render_failed status=%s path=%s", status_code, request.path)
        body = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{status_code} · RouteOps</title></head>"
            "<body style=\"font-family:system-ui,sans-serif;background:#f8f7f2;color:#172033;"
            "display:flex;min-height:100vh;align-items:center;justify-content:center\">"
            f"<main style='max-width:32rem;padding:2rem'><p style='font-size:.75rem;"
            "letter-spacing:.12em;text-transform:uppercase;color:#667085'>"
            f"Error {status_code}</p><h1 style='font-size:1.75rem;margin:.5rem 0'>{headline}</h1>"
            f"<p style='color:#667085'>{detail}</p>"
            "<p style='margin-top:2rem'><a href='/' style='color:#1264e8'>Return to RouteOps</a></p>"
            "</main></body></html>"
        )
        return HttpResponse(body, status=status_code, content_type="text/html; charset=utf-8")


def bad_request(request, exception=None):
    return _render_error(request, 400)


def permission_denied(request, exception=None):
    return _render_error(request, 403)


def page_not_found(request, exception=None):
    return _render_error(request, 404)


def server_error(request):
    # Django invokes this with only the request, and the exception has already
    # been logged by Django's own machinery.
    return _render_error(request, 500)
