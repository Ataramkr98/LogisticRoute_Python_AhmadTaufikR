import os
import sysconfig
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

def _configure_windows_geospatial_libraries():
    """Locate GDAL/GEOS and the PROJ database for GeoDjango on Windows.

    rasterio vendors GDAL and GEOS as hashed DLLs (for example
    ``gdal-360e5d11b6a02621294737b98153f3c0.dll``). Django's own discovery only
    probes unversioned names such as ``gdal310``, so the vendored copies are
    invisible to it unless we point Django at the exact files. The result is
    assigned to module scope because Django reads these as settings, and a
    local variable would silently be discarded.

    PROJ needs separate handling. Without ``PROJ_LIB`` pointing at a directory
    that contains ``proj.db``, any coordinate reprojection raises
    ``GDALException: OGR failure`` while plain spatial queries keep working,
    because PostGIS does that math server-side. The failure is therefore easy to
    ship unnoticed and hard to diagnose later.
    """
    global GDAL_LIBRARY_PATH, GEOS_LIBRARY_PATH

    if os.name != "nt":
        return

    site_packages = Path(sysconfig.get_paths()["purelib"])
    rasterio_libs = site_packages / "rasterio.libs"
    if not rasterio_libs.exists():
        return

    # Make co-located dependency DLLs resolvable before loading GDAL/GEOS.
    global _dll_directory_handle
    _dll_directory_handle = os.add_dll_directory(str(rasterio_libs))

    def _first(candidates):
        # Prefer an explicit override, then the vendored hashed build.
        configured = os.getenv(f"{candidates[0].upper()}_LIBRARY_PATH")
        if configured:
            return configured
        for pattern in candidates:
            matches = sorted(rasterio_libs.glob(pattern))
            if matches:
                return str(matches[0])
        return None

    gdal_path = _first(["GDAL", "gdal*.dll"])
    geos_path = _first(["GEOS_C", "geos_c*.dll"])
    if gdal_path:
        GDAL_LIBRARY_PATH = gdal_path
    if geos_path:
        GEOS_LIBRARY_PATH = geos_path

    # PROJ carries its own data directory alongside rasterio. Setting PROJ_LIB
    # before GDAL initialises is what lets reprojection find proj.db.
    if not os.getenv("PROJ_LIB"):
        for candidate in (
            site_packages / "rasterio" / "proj_data",
            site_packages / "rasterio.libs" / "proj_data",
        ):
            if (candidate / "proj.db").exists():
                os.environ["PROJ_LIB"] = str(candidate)
                break


GDAL_LIBRARY_PATH = os.getenv("GDAL_LIBRARY_PATH", "")
GEOS_LIBRARY_PATH = os.getenv("GEOS_LIBRARY_PATH", "")
_configure_windows_geospatial_libraries()

# Secure by default. The previous defaults ("true" / a literal key) meant a
# deployment that forgot one variable ran with the debug page, no HSTS, no SSL
# redirect, `testserver` appended to ALLOWED_HOSTS, and a publicly known signing
# key — a failure that is invisible until it is exploited. DJANGO_DEBUG must now
# be set explicitly to true for local work.
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "unsafe-local-development-key-only"
    else:
        raise RuntimeError(
            "DJANGO_SECRET_KEY is required when DJANGO_DEBUG is false. Generate one with "
            'python -c "from django.core.management.utils import get_random_secret_key as g; print(g())".'
        )
if not DEBUG and SECRET_KEY == "unsafe-local-development-key-only":
    raise RuntimeError(
        "DJANGO_SECRET_KEY is still the development placeholder. Generate a unique secret "
        'with python -c "from django.core.management.utils import get_random_secret_key as g; print(g())".'
    )
ADMIN_MFA_REQUIRED = os.getenv("ADMIN_MFA_REQUIRED", "true").lower() == "true"
ALLOWED_HOSTS = [x.strip() for x in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if x.strip()]
# Django's test client and the Playwright suite send ``Host: testserver``.
# Without it here every request is rejected with a 400 before authentication
# runs, which is indistinguishable from a broken login page. The entry is only
# added while DEBUG is on so a deployed environment keeps a strict host list.
if DEBUG and "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("testserver")
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "django_otp",
    "django_otp.plugins.otp_totp",
]
THIRD_PARTY_APPS = ["rest_framework", "rest_framework_simplejwt", "drf_spectacular", "django_filters"]
LOCAL_APPS = [
    "apps.common",
    "apps.accounts",
    "apps.organizations",
    "apps.depots",
    "apps.customers",
    "apps.fleet",
    "apps.drivers",
    "apps.orders",
    "apps.planning",
    "apps.routing",
    "apps.optimization",
    "apps.dispatch",
    "apps.tracking",
    "apps.proof_of_delivery",
    "apps.exceptions",
    "apps.reports",
    "apps.integrations",
    "apps.audit",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "apps.common.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "apps.common.middleware.CurrentOrganizationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.common.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
            # Supplies `{% if debug %}`, which the login template uses to hide the
            # seeded demo credentials outside development.
            "django.template.context_processors.debug",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ]},
    },
    {
        "BACKEND": "django.template.backends.jinja2.Jinja2",
        "DIRS": [BASE_DIR / "templates_jinja2"],
        "APP_DIRS": True,
        "OPTIONS": {
            "environment": "config.jinja2.environment",
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.common.context_processors.application_context",
            ],
        },
    },
]

DATABASE_URL = os.getenv("DATABASE_URL", "")

if DATABASE_URL:
    # Single-URL configuration (Neon, Railway, Fly, RDS, Docker Compose...).
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=int(os.getenv("DATABASE_CONN_MAX_AGE", "60")),
            conn_health_checks=True,
        )
    }
    DATABASES["default"]["ENGINE"] = "django.contrib.gis.db.backends.postgis"
    DATABASES["default"].setdefault("OPTIONS", {})
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "NAME": os.getenv("DATABASE_NAME", "postgres"),
            "USER": os.getenv("DATABASE_USER", "postgres"),
            "PASSWORD": os.getenv("DATABASE_PASSWORD", ""),
            "HOST": os.getenv("DATABASE_HOST", "127.0.0.1"),
            "PORT": os.getenv("DATABASE_PORT", "5432"),
            "CONN_MAX_AGE": int(os.getenv("DATABASE_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {"sslmode": os.getenv("DATABASE_SSLMODE", "prefer")},
            "TEST": {"NAME": os.getenv("TEST_DATABASE_NAME", "test_logistics")},
        }
    }

# Neon and most managed Postgres providers require TLS and close idle
# connections aggressively; keep statements short-lived so the pooler is happy.
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/app/dashboard/"
LOGOUT_REDIRECT_URL = "/login/"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English")]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_URL = "/private-media/"
MEDIA_ROOT = BASE_DIR / "media" / "private"
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
# Redis is an optional accelerator: rate limiting, progress streaming and Celery
# dispatch all tolerate its absence. Falling back to in-process memory keeps
# every page and API endpoint serving on a machine with no Redis, which matters
# for local demos and single-container deployments.
#
# The backend is chosen rather than hard-coded so that setting REDIS_URL in a
# deployed environment actually does something. An explicit CACHE_BACKEND always
# wins; otherwise production uses Redis and DEBUG stays in-process, because a
# developer should not have to install Redis to run the test suite.
CACHE_BACKEND = os.getenv("CACHE_BACKEND", "").strip()
if not CACHE_BACKEND:
    CACHE_BACKEND = (
        "django.core.cache.backends.locmem.LocMemCache"
        if DEBUG
        else "django.core.cache.backends.redis.RedisCache"
    )
CACHES = {
    "default": {
        "BACKEND": CACHE_BACKEND,
        "LOCATION": (
            os.getenv("CACHE_LOCATION", "routeops")
            if CACHE_BACKEND.endswith("LocMemCache")
            else REDIS_URL
        ),
        # A Redis outage must not turn every request into a 500. DRF throttles
        # read their counters from here and the readiness probe round-trips a
        # key, so the timeout has to be short and failures have to stay local.
        "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
        "KEY_PREFIX": os.getenv("CACHE_KEY_PREFIX", "routeops"),
    }
}
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/2")
# When no worker is running there is nothing to hand work to, so tasks execute
# inline in the request. This is the difference between "no Redis" being a
# supported configuration and every mutating endpoint returning a 500, so it
# defaults on in DEBUG and is driven by an explicit environment variable
# everywhere else. apps.common.dispatch.dispatch covers the case where eager is
# off and the broker is still unreachable.
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "true" if DEBUG else "false").lower() == "true"
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "600"))
# Fail fast rather than hanging: the Redis result backend otherwise retries its
# reconnect twenty times over ~20s before raising, which stalls the request
# that dispatched the task instead of returning promptly.
CELERY_TASK_PUBLISH_RETRY = False
CELERY_RESULT_BACKEND_ALWAYS_RETRY = False
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = False
CELERY_BROKER_TRANSPORT_OPTIONS = {"max_retries": 1, "socket_connect_timeout": 2}
CELERY_TASK_ROUTES = {
    "apps.orders.tasks.*": {"queue": "imports"},
    "apps.routing.tasks.geocode_address_task": {"queue": "geocoding"},
    "apps.routing.tasks.build_matrix_task": {"queue": "matrix"},
    "apps.optimization.tasks.*": {"queue": "optimization"},
    "apps.integrations.tasks.*": {"queue": "notifications"},
    "apps.reports.tasks.*": {"queue": "reports"},
    "apps.tracking.tasks.*": {"queue": "maintenance"},
}
CELERY_BEAT_SCHEDULE = {
    "prune-location-history": {
        "task": "apps.tracking.tasks.prune_location_history",
        "schedule": 86400,
    },
    "prune-idempotency-records": {
        "task": "apps.tracking.tasks.prune_idempotency_records",
        "schedule": 86400,
    },
}

API_PAGE_SIZE = int(os.getenv("API_PAGE_SIZE", "50"))
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "apps.common.authentication.DriverDeviceJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.common.api.exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["apps.common.authentication.ResilientUserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "1200/hour", "driver_location": "240/minute"},
    # Without this every list endpoint serialized its entire table. An
    # organization with a season of import history would return tens of
    # thousands of rows in one response, which is a denial-of-service lever
    # aimed at the client. Page size is overridable per request with ?page_size=.
    "DEFAULT_PAGINATION_CLASS": "apps.common.api.OrganizationPageNumberPagination",
    "PAGE_SIZE": int(os.getenv("API_PAGE_SIZE", "50")),
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(os.getenv("JWT_ACCESS_MINUTES", "15"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.getenv("JWT_REFRESH_DAYS", "7"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Logistics Route Optimization API",
    "VERSION": "1.0.0",
    "ENUM_NAME_OVERRIDES": {
        "CustomerStatusEnum": "apps.customers.models.Customer.Status",
        "VehicleStatusEnum": "apps.fleet.models.Vehicle.Status",
        "DriverStatusEnum": "apps.drivers.models.Driver.Status",
        "DriverShiftStatusEnum": "apps.drivers.models.DriverShift.Status",
        "OrderStatusEnum": "apps.orders.models.Order.Status",
        "ImportJobStatusEnum": "apps.orders.models.ImportJob.Status",
        "PlanningRunStatusEnum": "apps.planning.models.PlanningRun.Status",
        "RoutePlanStatusEnum": "apps.planning.models.RoutePlan.Status",
        "RouteStatusEnum": "apps.planning.models.Route.Status",
        "RouteStopStatusEnum": "apps.planning.models.RouteStop.Status",
        "OperationExceptionStatusEnum": "apps.exceptions.models.OperationException.Status",
    },
}

FIELD_ENCRYPTION_KEY = os.getenv("FIELD_ENCRYPTION_KEY", "")
MAP_TILE_URL = os.getenv("MAP_TILE_URL", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png")
MAP_ATTRIBUTION = os.getenv("MAP_ATTRIBUTION", "&copy; OpenStreetMap contributors")
GEOCODING_PROVIDER = os.getenv("GEOCODING_PROVIDER", "fake")
GEOCODING_BASE_URL = os.getenv("GEOCODING_BASE_URL", "https://nominatim.openstreetmap.org")
GEOCODING_USER_AGENT = os.getenv("GEOCODING_USER_AGENT", "logistics-route-optimization-local/1.0")
ROUTING_PROVIDER = os.getenv("ROUTING_PROVIDER", "fake")
ROUTING_BASE_URL = os.getenv("ROUTING_BASE_URL", "https://router.project-osrm.org")
SOLVER_TIME_LIMIT_SECONDS = int(os.getenv("SOLVER_TIME_LIMIT_SECONDS", "30"))
MAX_ORDERS_PER_RUN = int(os.getenv("MAX_ORDERS_PER_RUN", "1000"))
GPS_RETENTION_DAYS = int(os.getenv("GPS_RETENTION_DAYS", "90"))
POD_RETENTION_DAYS = int(os.getenv("POD_RETENTION_DAYS", "365"))

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "apps.common.logging.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "RouteOps <no-reply@routeops.local>")
# Password-reset and invitation mail is written to the log by default so a fresh
# checkout never needs an SMTP account. Point these at a real provider in a
# deployed environment.
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"

# Error reporting is opt-in. sentry-sdk is a hard dependency, but initialising it
# unconditionally would either send nothing (empty DSN) or, worse, ship local
# stack traces to a third party left behind in a copied .env file.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0"))
SENTRY_PROFILES_SAMPLE_RATE = float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0"))
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "development" if DEBUG else "production")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.redis import RedisIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=os.getenv("SENTRY_RELEASE"),
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
        # Request bodies can carry customer addresses and driver PII, so only
        # the shape of a failure is reported, never its payload.
        send_default_pii=False,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            RedisIntegration(),
            LoggingIntegration(level=os.getenv("LOG_LEVEL", "INFO"), event_level="ERROR"),
        ],
    )

