import uuid

from django.utils.deprecation import MiddlewareMixin


class RequestIDMiddleware(MiddlewareMixin):
    def process_request(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        request.request_id = incoming[:64] if incoming else str(uuid.uuid4())

    def process_response(self, request, response):
        response["X-Request-ID"] = getattr(request, "request_id", "")
        return response


def resolve_active_organization(request):
    """Set ``request.organization`` and ``request.membership`` from the caller.

    Session requests are resolved by ``CurrentOrganizationMiddleware`` before the
    view runs. Token requests cannot be: DRF only authenticates inside the view,
    so this is called again from ``DocumentedAPIView.initial`` once the bearer
    token has established the user. Idempotent, so calling it twice is safe.

    Returns the resolved membership, or ``None`` when the user has no active
    organization.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None

    memberships = user.organization_memberships.select_related("organization").filter(
        active=True,
        organization__status="ACTIVE",
    )
    session = getattr(request, "session", None)
    organization_id = session.get("organization_id") if session is not None else None
    membership = memberships.filter(organization_id=organization_id).first() if organization_id else None
    membership = membership or memberships.first()
    if membership:
        request.organization = membership.organization
        request.membership = membership
        if session is not None:
            session["organization_id"] = membership.organization_id
    return membership


class CurrentOrganizationMiddleware(MiddlewareMixin):
    """Attach the caller's active organization to the request.

    Session requests already carry an authenticated ``request.user`` by the time
    this middleware runs, because Django's session authentication is itself
    middleware. Token requests do not: DRF only authenticates inside the view,
    long after middleware has finished, so ``request.user`` is still anonymous
    here and the organization would never be resolved.

    That gap used to make every JWT-protected endpoint reject its own caller — a
    driver could present a perfectly valid token and still receive 403 from
    ``/api/v1/driver/routes/today/`` because ``request.organization`` was None
    and ``IsDriver`` could not match the driver's organization. Bearer requests
    are therefore left for ``resolve_active_organization`` to handle once DRF
    has established the user.
    """

    _BEARER_PREFIX = "Bearer "

    def process_request(self, request):
        request.organization = None
        request.membership = None

        # A token request is authenticated by DRF inside the view; resolving now
        # would see an anonymous user. DocumentedAPIView.initial finishes it.
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith(self._BEARER_PREFIX):
            return

        resolve_active_organization(request)


class SecurityHeadersMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        response.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob: https:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'",
        )
        response.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.setdefault("Permissions-Policy", "camera=(self), geolocation=(self), microphone=()")
        return response
