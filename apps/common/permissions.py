from rest_framework.permissions import BasePermission


class HasOrganization(BasePermission):
    message = "An active organization membership is required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.organization)


class IsDriver(BasePermission):
    message = "An associated driver account is required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and hasattr(request.user, "driver_profile")
            and request.user.driver_profile.organization_id == getattr(request.organization, "id", None)
        )
