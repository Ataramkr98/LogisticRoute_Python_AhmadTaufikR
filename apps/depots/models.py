from django.contrib.gis.db import models

from apps.common.models import TimeStampedModel


class Depot(TimeStampedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="depots")
    name = models.CharField(max_length=180)
    code = models.CharField(max_length=40)
    address_text = models.TextField()
    location = models.PointField(srid=4326)
    service_area = models.PolygonField(srid=4326, null=True, blank=True, spatial_index=False)
    timezone = models.CharField(max_length=64, default="UTC")
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="uq_depot_code")]

    def __str__(self):
        return f"{self.code} — {self.name}"


class RestrictedZone(TimeStampedModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="restricted_zones")
    name = models.CharField(max_length=180)
    polygon = models.PolygonField(srid=4326)
    prohibited_vehicle_types = models.JSONField(default=list, blank=True)
    required_skills = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

