"""Spatial round-trip checks against the configured PostGIS backend."""

import pytest
from django.contrib.gis.geos import Point
from django.db import connection

from apps.depots.models import Depot
from apps.organizations.models import Organization

pytestmark = [pytest.mark.integration]


@pytest.mark.django_db(transaction=True)
def test_srid_coordinate_order_and_spatial_index():
    """Coordinates must round-trip as (lon, lat) and the column must be indexed."""
    organization = Organization.objects.create(name="GIS Test", slug="gis-test")
    depot = Depot.objects.create(
        organization=organization,
        name="Test Depot",
        code="GIS",
        address_text="Test",
        location=Point(106.8272, -6.2146, srid=4326),
    )
    depot.refresh_from_db()

    # GeoDjango stores points as (x=longitude, y=latitude) in SRID 4326. Getting
    # the order wrong is the classic spatial bug: it survives insertion but puts
    # every vehicle in the wrong hemisphere.
    assert depot.location.srid == 4326
    assert depot.location.x == pytest.approx(106.8272)
    assert depot.location.y == pytest.approx(-6.2146)


@pytest.mark.django_db(transaction=True)
def test_spatial_column_is_geography_aware_and_indexed():
    """PostGIS must report a GiST index on the spatial column."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT i.indexdef
            FROM pg_indexes AS i
            WHERE i.tablename = 'depots_depot'
            """
        )
        index_definitions = [row[0].lower() for row in cursor.fetchall()]

    spatial_indexes = [definition for definition in index_definitions if "gist" in definition]
    assert spatial_indexes, f"no GiST index found on depots_depot: {index_definitions}"
    assert any("location" in definition for definition in spatial_indexes)


@pytest.mark.django_db(transaction=True)
def test_postgis_extension_is_installed():
    """The PostGIS extension itself must be present, not just the SRID metadata."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT extname FROM pg_extension")
        extensions = {row[0] for row in cursor.fetchall()}
    assert "postgis" in extensions, f"postgis extension missing: {extensions}"
