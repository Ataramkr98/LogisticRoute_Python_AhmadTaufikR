import logging

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.throttling import UserRateThrottle
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.drivers.models import DriverDevice

logger = logging.getLogger(__name__)


class ResilientUserRateThrottle(UserRateThrottle):
    """Rate limit that degrades open instead of failing closed.

    DRF throttles read their counters from the cache backend. With Redis
    configured, a Redis outage therefore turns every API request into a 500 —
    including unauthenticated endpoints such as the OpenAPI schema, which makes
    the whole API surface unavailable. Throttling is a protection mechanism, not
    a correctness requirement, so when the cache cannot be reached we allow the
    request through and log it rather than denying service.
    """

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.warning(
                "rate_limit_cache_unavailable scope=%s path=%s",
                getattr(self, "scope", None),
                request.path,
            )
            return True


class DriverDeviceJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        device_id = validated_token.get("device_id")
        if device_id:
            active = DriverDevice.objects.filter(
                driver__user=user,
                device_id=device_id,
                revoked_at__isnull=True,
            ).exists()
            if not active:
                raise AuthenticationFailed("Driver device has been revoked")
        return user


class DriverDeviceJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "apps.common.authentication.DriverDeviceJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
