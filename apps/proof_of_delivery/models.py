from django.conf import settings
from django.contrib.gis.db import models

from apps.common.models import TimeStampedModel


def pod_upload_path(instance, filename):
    return f"pod/{instance.route_stop_id}/{filename}"


class ProofOfDelivery(TimeStampedModel):
    route_stop = models.OneToOneField("planning.RouteStop", on_delete=models.CASCADE, related_name="proof_of_delivery")
    recipient_name = models.CharField(max_length=180, blank=True)
    recipient_relation = models.CharField(max_length=100, blank=True)
    signature_file = models.FileField(upload_to=pod_upload_path, null=True, blank=True)
    photo_file = models.ImageField(upload_to=pod_upload_path, null=True, blank=True)
    note = models.TextField(blank=True)
    captured_at = models.DateTimeField()
    captured_location = models.PointField(srid=4326, null=True, blank=True, spatial_index=False)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="verified_pods")
    verified_at = models.DateTimeField(null=True, blank=True)

