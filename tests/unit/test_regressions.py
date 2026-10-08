from types import SimpleNamespace

import pytest
from rest_framework.exceptions import ValidationError

from apps.common.api_views import OptimisticVersionMixin
from apps.common.serializers import PolygonGeoJSONField, _validate_organization
from apps.tracking.services import DriverActionConflict, _check_version, _location


class VersionHarness(OptimisticVersionMixin):
    def __init__(self, if_match):
        self.request = SimpleNamespace(headers={"If-Match": if_match})


@pytest.mark.parametrize("header", ['W/"7"', '"7"', "7"])
def test_if_match_accepts_weak_strong_and_unquoted_versions(header):
    VersionHarness(header)._check_if_match(SimpleNamespace(version=7))


def test_if_match_rejects_a_stale_version():
    with pytest.raises(ValidationError, match="Version conflict"):
        VersionHarness('W/"6"')._check_if_match(SimpleNamespace(version=7))


def test_driver_version_validation_rejects_invalid_or_stale_values():
    instance = SimpleNamespace(version=4)
    with pytest.raises(DriverActionConflict, match="must be an integer"):
        _check_version(instance, "not-a-version")
    with pytest.raises(DriverActionConflict, match="current version is 4"):
        _check_version(instance, 3)


@pytest.mark.parametrize(
    "payload",
    [
        {"longitude": 181, "latitude": 0},
        {"longitude": 0, "latitude": -91},
        {"longitude": "east", "latitude": 10},
    ],
)
def test_driver_locations_reject_invalid_wgs84_coordinates(payload):
    with pytest.raises(DriverActionConflict):
        _location(payload)


def test_organization_validation_rejects_cross_tenant_relationships():
    organization = SimpleNamespace(pk=10)
    with pytest.raises(ValidationError, match="active organization"):
        _validate_organization(organization, depot=SimpleNamespace(organization_id=11))


def test_geojson_polygon_field_accepts_closed_wgs84_rings():
    field = PolygonGeoJSONField()
    polygon = field.to_internal_value(
        {
            "type": "Polygon",
            "coordinates": [
                [[106.8, -6.3], [106.9, -6.3], [106.9, -6.2], [106.8, -6.3]]
            ],
        }
    )
    assert polygon.srid == 4326
    assert polygon.geom_type == "Polygon"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ({"idr_per_objective_unit": 100}, 100.0),
        ({"idr_per_objective_unit": "250"}, 250.0),
        ({}, 100.0),
        ({"idr_per_objective_unit": 0}, 100.0),
        ({"idr_per_objective_unit": -5}, 100.0),
        ({"idr_per_objective_unit": None}, 100.0),
        ({"idr_per_objective_unit": "not-a-number"}, 100.0),
    ],
)
def test_objective_unit_rate_is_read_defensively(configured, expected):
    from apps.optimization.services import _objective_units

    assert _objective_units(configured) == expected


def test_vehicle_fixed_cost_is_normalised_into_objective_units():
    """Fixed cost shares the arc cost's unit, so it cannot dwarf travel cost.

    A currency fixed cost left unscaled (or multiplied by 100) makes opening a
    vehicle look more expensive than the penalty for dropping an order, and the
    solver then discards orders it could easily serve.
    """
    from decimal import Decimal

    from apps.optimization.services import _vehicle_input

    entry = SimpleNamespace(
        vehicle=SimpleNamespace(
            pk=1,
            vehicle_type="VAN",
            capacity_weight_kg=Decimal("900"),
            capacity_volume_m3=Decimal("8"),
            capacity_package_count=80,
            max_stops=35,
            max_route_duration_seconds=36_000,
            skills_json=["STANDARD"],
            fixed_cost=Decimal("150000"),
        ),
        driver_id=None,
    )
    input_value = _vehicle_input(SimpleNamespace(service_date=None, depot=None), entry, None, 100.0)
    assert input_value.fixed_cost == 1500
