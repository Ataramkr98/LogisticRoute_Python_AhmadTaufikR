"""Shared pytest configuration.

The suite deliberately runs with ``DJANGO_DEBUG=false`` — the same posture as a
deployed environment — so that the secure defaults are exercised on every run
rather than only in production. Two settings have to be relaxed for that to work,
because they describe the network the app is reached over rather than any
behaviour of the app itself:

* ``ALLOWED_HOSTS`` — Django's test client sends ``Host: testserver``, which is
  only appended automatically while DEBUG is on. Without this every request in
  the suite would be rejected with 400 before it reached a view.
* ``SECURE_SSL_REDIRECT`` — the test client speaks plain HTTP, so every request
  would be answered 301.

Neither relaxation weakens anything the tests assert: the checks that matter for
security (``SECURE_HSTS_SECONDS``, session and CSRF cookie flags, CSP headers)
stay active.

A separate helper resets a leftover test database, which is a recurring problem
on shared managed Postgres where an interrupted run leaves the database and its
session behind.
"""

import pytest
from django.conf import settings


def pytest_configure(config):
    # Applied before any test module imports Django models.
    settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]
    settings.SECURE_SSL_REDIRECT = False


@pytest.fixture
def relaxed_network_settings(settings):
    """Per-test access to the same relaxations, for modules that override env."""
    settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]
    settings.SECURE_SSL_REDIRECT = False
    return settings
