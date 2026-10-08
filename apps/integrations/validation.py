"""Outbound URL safety checks.

Kept in its own module because both the serializer (which validates at save
time) and the delivery task (which re-validates immediately before sending)
need it. Defining it in ``services`` would create a cycle: services imports the
task, and the task imports the validator back.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError


def validate_webhook_url(url):
    """Reject webhook targets that would let the server reach its own network.

    A subscription URL is operator-supplied and the delivery task performs an
    outbound POST with it. Without validation, anyone able to create a
    subscription could point it at ``http://169.254.169.254/`` (cloud instance
    credentials), ``http://127.0.0.1:5432/`` or an internal service and read the
    response back through ``WebhookDelivery.response_body`` — server-side request
    forgery with the response exfiltrated.

    Only public addresses over HTTPS are allowed. The hostname is resolved here
    so a name that maps to a private address is rejected at save time, which is
    the only point where a clear error can be shown to the operator; the task
    re-checks before sending, because DNS can change after validation.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValidationError("Webhook URLs must use https so payloads are not sent in clear text.")
    host = parts.hostname
    if not host:
        raise ValidationError("Webhook URL must include a hostname.")

    # Literal addresses never need a DNS lookup.
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise ValidationError(f"Webhook host {host!r} could not be resolved.") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue

    if not addresses:
        raise ValidationError(f"Webhook host {host!r} did not resolve to any usable address.")
    for address in addresses:
        if not address.is_global:
            raise ValidationError(
                f"Webhook host {host!r} resolves to {address}, which is not a public address. "
                "Private, loopback and link-local targets are refused."
            )
    return url
