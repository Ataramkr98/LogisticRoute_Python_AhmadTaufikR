from django.contrib.gis.db import models

from apps.common.models import TimeStampedModel


class DriverEvent(TimeStampedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="driver_events")
    route = models.ForeignKey("planning.Route", on_delete=models.CASCADE, related_name="events")
    route_stop = models.ForeignKey("planning.RouteStop", on_delete=models.SET_NULL, null=True, blank=True, related_name="events")
    driver = models.ForeignKey("drivers.Driver", on_delete=models.PROTECT, related_name="events")
    event_type = models.CharField(max_length=40)
    occurred_at = models.DateTimeField()
    location = models.PointField(srid=4326, null=True, blank=True, spatial_index=False)
    accuracy_m = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    payload_json = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=100)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["driver", "idempotency_key"], name="uq_driver_event_key")]
        indexes = [models.Index(fields=["route", "occurred_at"])]
        ordering = ["-occurred_at"]


class VehicleLocation(models.Model):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="vehicle_locations")
    vehicle = models.ForeignKey("fleet.Vehicle", on_delete=models.CASCADE, related_name="locations")
    driver = models.ForeignKey("drivers.Driver", on_delete=models.SET_NULL, null=True, blank=True, related_name="locations")
    route = models.ForeignKey("planning.Route", on_delete=models.SET_NULL, null=True, blank=True, related_name="locations")
    recorded_at = models.DateTimeField()
    location = models.PointField(srid=4326)
    speed_kph = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    heading = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    accuracy_m = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=40, default="driver_web")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["vehicle", "recorded_at"])]
        ordering = ["-recorded_at"]

