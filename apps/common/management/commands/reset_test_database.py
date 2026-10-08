"""Drop a leftover test database.

Shared managed Postgres providers (Neon, Supabase, a shared development server)
behave differently from a local server: when a test run is interrupted, the
`test_<name>` database and its session survive. Every later run then fails during
setup with

    database "test_logistics" already exists
    database "test_logistics" is being accessed by other users

and the failure message points at the fixtures rather than at the real cause.
This script terminates the stale sessions and drops the database so the next run
starts clean.

Usage:
    python manage.py reset_test_database          # honours TEST_DATABASE_NAME
    python manage.py reset_test_database --name test_logistics
    python manage.py reset_test_database --keep   # list only, drop nothing
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = "Terminate open sessions on the test database and drop it."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="", help="Database name; defaults to TEST_DATABASE_NAME.")
        parser.add_argument("--keep", action="store_true", help="Report only, do not drop.")

    def handle(self, *args, **options):
        # The test database name is derived from the configured one, matching
        # Django's own prefixing, so this cannot accidentally target the real
        # database.
        target = options["name"] or getattr(settings, "TEST_DATABASE_NAME", "") or "test_" + (
            settings.DATABASES["default"]["NAME"] or "postgres"
        )
        if not target.startswith("test"):
            self.stderr.write(
                self.style.ERROR(
                    f"Refusing to touch {target!r}: a test database name must start with 'test'."
                )
            )
            return

        alias = next(iter(settings.DATABASES))
        connection = connections[alias]
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    [target],
                )
                terminated = cursor.rowcount
                if options["keep"]:
                    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [target])
                    exists = cursor.fetchone() is not None
                    self.stdout.write(
                        f"{target}: {terminated} session(s) terminated; "
                        f"database {'present' if exists else 'absent'}. Nothing dropped (--keep)."
                    )
                    return
                cursor.execute(f'DROP DATABASE IF EXISTS "{target}"')
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Dropped {target} after terminating {terminated} session(s)."
                    )
                )
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            self.stderr.write(self.style.ERROR(f"Could not reset {target}: {exc}"))
