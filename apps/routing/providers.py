import hashlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx
from django.conf import settings


@dataclass(frozen=True)
class Coordinate:
    longitude: float
    latitude: float


@dataclass(frozen=True)
class GeocodeResult:
    coordinate: Coordinate
    formatted_address: str
    confidence: float
    raw: dict


@dataclass(frozen=True)
class MatrixResult:
    distances_m: list[list[int]]
    durations_s: list[list[int]]
    provider: str


class ProviderError(RuntimeError):
    pass


class Geocoder(ABC):
    name: str

    @abstractmethod
    def geocode(self, address: str) -> GeocodeResult: ...


class Router(ABC):
    name: str

    @abstractmethod
    def matrix(self, points: list[Coordinate], profile: str = "driving") -> MatrixResult: ...

    @abstractmethod
    def route(self, points: list[Coordinate], profile: str = "driving") -> dict: ...


def _haversine_m(a: Coordinate, b: Coordinate) -> int:
    radius = 6_371_000
    phi1, phi2 = math.radians(a.latitude), math.radians(b.latitude)
    d_phi = math.radians(b.latitude - a.latitude)
    d_lambda = math.radians(b.longitude - a.longitude)
    value = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return int(2 * radius * math.atan2(math.sqrt(value), math.sqrt(1 - value)))


class FakeGeocoder(Geocoder):
    name = "fake"

    def geocode(self, address: str) -> GeocodeResult:
        digest = hashlib.sha256(address.strip().lower().encode()).digest()
        lon = 106.80 + int.from_bytes(digest[:2], "big") / 65535 * 0.18
        lat = -6.30 + int.from_bytes(digest[2:4], "big") / 65535 * 0.18
        return GeocodeResult(Coordinate(lon, lat), address.strip(), 0.95, {"source": "deterministic"})


class NominatimGeocoder(Geocoder):
    name = "nominatim"

    def geocode(self, address: str) -> GeocodeResult:
        try:
            response = httpx.get(
                f"{settings.GEOCODING_BASE_URL.rstrip('/')}/search",
                params={"q": address, "format": "jsonv2", "limit": 1, "addressdetails": 1},
                headers={"User-Agent": settings.GEOCODING_USER_AGENT},
                timeout=20,
            )
            response.raise_for_status()
            results = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Nominatim request failed: {exc}") from exc
        if not results:
            raise ProviderError("Address was not found")
        result = results[0]
        return GeocodeResult(
            Coordinate(float(result["lon"]), float(result["lat"])),
            result.get("display_name", address),
            min(1.0, float(result.get("importance", 0.5))),
            result,
        )


class FakeRouter(Router):
    name = "fake"
    speed_mps = 35_000 / 3600

    def matrix(self, points, profile="driving"):
        distances = [[0 for _ in points] for _ in points]
        durations = [[0 for _ in points] for _ in points]
        for i, source in enumerate(points):
            for j, target in enumerate(points):
                if i != j:
                    distance = int(_haversine_m(source, target) * 1.25)
                    distances[i][j] = distance
                    durations[i][j] = max(1, int(distance / self.speed_mps))
        return MatrixResult(distances, durations, self.name)

    def route(self, points, profile="driving"):
        distance = sum(_haversine_m(a, b) for a, b in zip(points, points[1:], strict=False))
        return {
            "distance_m": distance,
            "duration_s": int(distance / self.speed_mps),
            "coordinates": [[point.longitude, point.latitude] for point in points],
            "provider_route_id": "",
        }


class OSRMRouter(Router):
    name = "osrm"

    @staticmethod
    def _coordinates(points):
        return ";".join(f"{point.longitude},{point.latitude}" for point in points)

    def matrix(self, points, profile="driving"):
        if len(points) > 100:
            raise ProviderError("The configured OSRM adapter supports at most 100 points per request")
        url = f"{settings.ROUTING_BASE_URL.rstrip('/')}/table/v1/{profile}/{self._coordinates(points)}"
        try:
            response = httpx.get(url, params={"annotations": "distance,duration"}, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"OSRM matrix request failed: {exc}") from exc
        if payload.get("code") != "Ok" or payload.get("distances") is None:
            raise ProviderError(payload.get("message", "OSRM returned an invalid matrix"))
        distances = [[int(value) if value is not None else 10**9 for value in row] for row in payload["distances"]]
        durations = [[int(value) if value is not None else 10**9 for value in row] for row in payload["durations"]]
        return MatrixResult(distances, durations, self.name)

    def route(self, points, profile="driving"):
        url = f"{settings.ROUTING_BASE_URL.rstrip('/')}/route/v1/{profile}/{self._coordinates(points)}"
        try:
            response = httpx.get(url, params={"overview": "full", "geometries": "geojson"}, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"OSRM route request failed: {exc}") from exc
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise ProviderError(payload.get("message", "OSRM returned no route"))
        route = payload["routes"][0]
        return {
            "distance_m": int(route["distance"]),
            "duration_s": int(route["duration"]),
            "coordinates": route["geometry"]["coordinates"],
            "provider_route_id": "",
        }


def get_geocoder() -> Geocoder:
    return NominatimGeocoder() if settings.GEOCODING_PROVIDER.lower() == "nominatim" else FakeGeocoder()


def get_router() -> Router:
    return OSRMRouter() if settings.ROUTING_PROVIDER.lower() == "osrm" else FakeRouter()

