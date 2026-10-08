from django.http import HttpResponseRedirect
from django.urls import path, reverse

from . import web_views


def home(request):
    return HttpResponseRedirect(reverse("dashboard"))


urlpatterns = [
    path("", home, name="home"),
    path("no-organization/", web_views.no_organization, name="no-organization"),
    path("app/dashboard/", web_views.dashboard, name="dashboard"),
    path("app/orders/", web_views.orders_list, name="order-list"),
    path("app/orders/new/", web_views.order_create, name="order-create"),
    path("app/orders/import/", web_views.order_import, name="order-import"),
    path("app/orders/<int:order_id>/", web_views.order_detail, name="order-detail"),
    path("app/planning/new/", web_views.planning_new, name="planning-new"),
    path("app/planning/optimization/<int:run_id>/", web_views.planning_progress, name="planning-progress"),
    path("app/planning/review/<int:plan_id>/", web_views.planning_review, name="planning-review"),
    path("app/routes/", web_views.route_list, name="route-list"),
    path("app/routes/<int:route_id>/", web_views.route_detail, name="route-detail"),
    path("app/live-map/", web_views.live_map, name="live-map"),
    path("app/drivers/", web_views.fleet_page, {"resource": "drivers"}, name="driver-list"),
    path("app/vehicles/", web_views.fleet_page, {"resource": "vehicles"}, name="vehicle-list"),
    path("app/depots/", web_views.fleet_page, {"resource": "depots"}, name="depot-list"),
    path("app/exceptions/", web_views.exceptions_page, name="exceptions"),
    path("app/reports/", web_views.reports_page, name="reports"),
    path("app/integrations/", web_views.integrations_page, name="integrations"),
    path("app/settings/", web_views.settings_page, name="settings"),
    path("driver/today/", web_views.driver_today, name="driver-today"),
    path("driver/route/<int:route_id>/", web_views.driver_route, name="driver-route-page"),
    path("driver/stop/<int:stop_id>/", web_views.driver_stop, name="driver-stop-page"),
    path("driver/pod/<int:stop_id>/", web_views.driver_pod, name="driver-pod-page"),
    path("driver/history/", web_views.driver_history, name="driver-history"),
    path("private-media/<path:file_path>", web_views.private_media, name="private-media"),
    path("service-worker.js", web_views.service_worker, name="service-worker"),
]
