from django.contrib.gis.db import models

from apps.common.fields import EncryptedTextField
from apps.common.models import TimeStampedModel


class Customer(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="customers")
    external_ref = models.CharField(max_length=100)
    name = models.CharField(max_length=180)
    email_encrypted = EncryptedTextField(blank=True, default="")
    phone_encrypted = EncryptedTextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status, default=Status.ACTIVE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "external_ref"], name="uq_customer_ref")]

    def __str__(self):
        return self.name


class Address(TimeStampedModel):
    class GeocodeStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VALID = "VALID", "Valid"
        AMBIGUOUS = "AMBIGUOUS", "Ambiguous"
        INVALID = "INVALID", "Invalid"
        FAILED = "FAILED", "Failed"

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="addresses")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="addresses")
    label = models.CharField(max_length=100, blank=True)
    line1 = models.CharField(max_length=255)
    line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120)
    region = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=24, blank=True)
    country_code = models.CharField(max_length=2)
    formatted_address = models.TextField(blank=True)
    location = models.PointField(srid=4326, null=True, blank=True, spatial_index=False)
    geocode_status = models.CharField(max_length=16, choices=GeocodeStatus, default=GeocodeStatus.PENDING)
    geocode_provider = models.CharField(max_length=40, blank=True)
    geocode_confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    access_notes = models.TextField(blank=True)

    @property
    def one_line(self):
        return self.formatted_address or ", ".join(filter(None, [self.line1, self.city, self.region, self.country_code]))

    def __str__(self):
        return self.one_line


class GeocodeCache(TimeStampedModel):
    normalized_hash = models.CharField(max_length=64)
    provider = models.CharField(max_length=40)
    formatted_address = models.TextField()
    location = models.PointField(srid=4326)
    confidence = models.DecimalField(max_digits=5, decimal_places=4)
    raw_response = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["normalized_hash", "provider"], name="uq_geocode_cache")]

