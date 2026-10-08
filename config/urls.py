from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.common import views as common_views

# Registered before the catch-all include so a mistyped path reaches the branded
# 404 page instead of Django's default plain-text response.
handler400 = "apps.common.error_views.bad_request"
handler403 = "apps.common.error_views.permission_denied"
handler404 = "apps.common.error_views.page_not_found"
handler500 = "apps.common.error_views.server_error"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("health/live/", common_views.liveness, name="liveness"),
    path("health/ready/", common_views.readiness, name="readiness"),
    path("metrics/", common_views.metrics, name="metrics"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/v1/", include("apps.common.api_urls")),
    path("", include("apps.common.web_urls")),
]

