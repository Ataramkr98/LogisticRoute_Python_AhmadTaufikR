from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse, JsonResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis import Redis


def liveness(request):
    return JsonResponse({"status": "ok"})


def readiness(request):
    """Report whether this instance can serve traffic.

    The database is the only hard requirement. Cache and broker are documented
    as optional accelerators: throttling degrades open, progress falls back to
    polling, and task dispatch falls back to inline execution. Failing the check
    on those would make an orchestrator kill a container that is serving
    perfectly well, so they are reported but never fail the probe. A load
    balancer still learns about the degradation from the response body.
    """
    checks = {"database": False, "cache": False, "broker": False}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            checks["database"] = cursor.fetchone()[0] == 1
    except Exception:
        pass
    try:
        cache.set("readiness", "ok", timeout=5)
        checks["cache"] = cache.get("readiness") == "ok"
    except Exception:
        pass
    try:
        broker = Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2)
        checks["broker"] = bool(broker.ping())
        broker.close()
    except Exception:
        pass

    status = 200 if checks["database"] else 503
    payload = {
        "status": "ok" if status == 200 else "unavailable",
        "checks": checks,
        "degraded": sorted(name for name, ok in checks.items() if not ok and name != "database"),
    }
    return JsonResponse(payload, status=status)


def metrics(request):
    return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)

