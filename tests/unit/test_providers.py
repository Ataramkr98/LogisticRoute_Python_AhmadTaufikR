from apps.routing.providers import Coordinate, FakeGeocoder, FakeRouter


def test_fake_geocoder_is_deterministic_and_wgs84_valid():
    first = FakeGeocoder().geocode("Jl. Senopati No. 12, Jakarta")
    second = FakeGeocoder().geocode("Jl. Senopati No. 12, Jakarta")
    assert first.coordinate == second.coordinate
    assert -180 <= first.coordinate.longitude <= 180
    assert -90 <= first.coordinate.latitude <= 90


def test_fake_matrix_has_zero_diagonal_and_symmetric_road_estimates():
    points = [Coordinate(106.82, -6.21), Coordinate(106.88, -6.18), Coordinate(106.79, -6.27)]
    result = FakeRouter().matrix(points)
    assert [result.distances_m[index][index] for index in range(3)] == [0, 0, 0]
    assert result.distances_m[0][1] == result.distances_m[1][0]
    assert result.durations_s[1][2] > 0

