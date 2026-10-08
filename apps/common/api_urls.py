from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from . import api_views

router = DefaultRouter()
router.register("depots", api_views.DepotViewSet)
router.register("restricted-zones", api_views.RestrictedZoneViewSet)
router.register("customers", api_views.CustomerViewSet)
router.register("addresses", api_views.AddressViewSet)
router.register("vehicles", api_views.VehicleViewSet)
router.register("drivers", api_views.DriverViewSet)
router.register("driver-shifts", api_views.DriverShiftViewSet)
router.register("orders", api_views.OrderViewSet)
router.register("import-jobs", api_views.ImportJobViewSet)
router.register("planning-profiles", api_views.PlanningProfileViewSet)
router.register("planning-runs", api_views.PlanningRunViewSet)
router.register("route-plans", api_views.RoutePlanViewSet)
router.register("routes", api_views.RouteViewSet)
router.register("route-stops", api_views.RouteStopViewSet)
router.register("exceptions", api_views.ExceptionViewSet)
router.register("report-exports", api_views.ReportExportViewSet)
router.register("webhooks", api_views.WebhookSubscriptionViewSet)

urlpatterns = [
    path("auth/token/", api_views.DriverTokenView.as_view(), name="token-obtain"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("dashboard/kpis/", api_views.DashboardAPIView.as_view(), name="dashboard-kpis"),
    path("driver/routes/today/", api_views.DriverRoutesTodayAPIView.as_view(), name="driver-routes-today"),
    path("driver/routes/<int:route_id>/", api_views.DriverRouteAPIView.as_view(), name="driver-route"),
    path("driver/routes/<int:route_id>/start/", api_views.DriverRouteStartAPIView.as_view(), name="driver-route-start"),
    path(
        "driver/stops/<int:stop_id>/<str:operation>/",
        api_views.DriverStopActionAPIView.as_view(),
        name="driver-stop-action",
    ),
    path("driver/stops/<int:stop_id>/pod/", api_views.DriverPODAPIView.as_view(), name="driver-pod"),
    path("driver/location-events/", api_views.DriverLocationAPIView.as_view(), name="driver-location"),
    path("driver/offline-batch/", api_views.DriverOfflineBatchAPIView.as_view(), name="driver-offline-batch"),
    path(
        "driver/devices/<uuid:device_id>/revoke/",
        api_views.DriverDeviceRevokeAPIView.as_view(),
        name="driver-device-revoke",
    ),
    path("live/routes/", api_views.LiveRoutesAPIView.as_view(), name="live-routes"),
    path("live/vehicles/", api_views.LiveVehiclesAPIView.as_view(), name="live-vehicles"),
    path("routes/<int:route_id>/events/", api_views.RouteEventsAPIView.as_view(), name="route-events"),
    path("", include(router.urls)),
]
