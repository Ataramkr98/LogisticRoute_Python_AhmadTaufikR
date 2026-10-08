from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel


class Organization(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"

    name = models.CharField(max_length=180)
    slug = models.SlugField(max_length=100, unique=True)
    timezone = models.CharField(max_length=64, default="UTC")
    default_currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=20, choices=Status, default=Status.ACTIVE)

    def __str__(self):
        return self.name


class OrganizationMembership(TimeStampedModel):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Organization Admin"
        OPERATIONS_MANAGER = "OPERATIONS_MANAGER", "Operations Manager"
        DISPATCHER = "DISPATCHER", "Dispatcher"
        FLEET_MANAGER = "FLEET_MANAGER", "Fleet Manager"
        DRIVER = "DRIVER", "Driver"
        CUSTOMER_SERVICE = "CUSTOMER_SERVICE", "Customer Service"
        AUDITOR = "AUDITOR", "Auditor"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organization_memberships")
    role = models.CharField(max_length=32, choices=Role)
    depots = models.ManyToManyField("depots.Depot", blank=True, related_name="authorized_memberships")
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="uq_org_membership")]

    def __str__(self):
        return f"{self.user} · {self.organization}"

