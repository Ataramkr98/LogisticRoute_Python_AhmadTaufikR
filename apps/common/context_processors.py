from django.conf import settings


def application_context(request):
    return {
        "current_organization": getattr(request, "organization", None),
        "current_membership": getattr(request, "membership", None),
        "map_tile_url": settings.MAP_TILE_URL,
        "map_attribution": settings.MAP_ATTRIBUTION,
    }

