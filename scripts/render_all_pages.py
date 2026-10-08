"""Render every page in the project as an authenticated operator.

Walks the URL configuration so a new route is covered automatically, then hits
each one and reports the status and any server-side traceback. This is the
broadest smoke test available without a browser: it exercises routing,
permission checks, view logic, querysets and template compilation together.

Run with:  python scripts/render_all_pages.py
"""
import os
import re
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import URLPattern, URLResolver, get_resolver  # noqa: E402

TRACEBACK_MARKERS = ("Traceback (most recent call last)", "Internal Server Error")


def collect_named_urls(resolver, prefix=""):
    """Yield (name, path) for every named, parameterised route."""
    found = []
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            found.extend(collect_named_urls(entry, prefix + str(entry.pattern)))
        elif isinstance(entry, URLPattern) and entry.name:
            route = prefix + str(entry.pattern)
            # Concrete URLs only: fill the first capture group so pages that need
            # an id render against real data rather than being skipped.
            concrete = re.sub(r"\(\?P<[a-z_]+>[^)]*\)[^/]*", "1", route)
            concrete = re.sub(r"<int:[a-z_]+>|<slug:[a-z_]+>|<uuid:[a-z_]+>", "1", concrete)
            concrete = concrete.replace("^", "").replace("$", "")
            if "?" in concrete or "(" in concrete:
                continue
            found.append((entry.name, "/" + concrete.lstrip("/")))
    return found


def main():
    user = get_user_model().objects.get(email="demo@example.com")
    client = Client(raise_request_exception=False)
    client.force_login(user)

    urls = collect_named_urls(get_resolver())
    seen, targets = set(), []
    for name, path in urls:
        if path in seen or path.startswith("/admin"):
            continue
        seen.add(path)
        targets.append((name, path))

    failures, redirects = [], []
    for name, path in sorted(targets, key=lambda item: item[1]):
        response = client.get(path, follow=False)
        status = response.status_code
        # Streaming responses (the SSE progress endpoint, FileResponse) have no
        # `.content`; there is nothing to scan for them and reading them would
        # block on an open connection.
        if getattr(response, "streaming", False):
            print(f"  [stream] {status}  {name:<28} {path}")
            continue
        body = response.content.decode("utf-8", "replace") if status == 200 else ""
        marker = next((m for m in TRACEBACK_MARKERS if m in body), None)
        if status >= 500 or marker:
            failures.append((name, path, status, marker))
            print(f"  [FAIL] {status}  {name:<28} {path}  {marker or ''}")
        elif status in (301, 302):
            redirects.append((name, path, status))
            print(f"  [redir] {status}  {name:<28} {path}")
        else:
            print(f"  [ok]   {status}  {name:<28} {path}  ({len(body)} bytes)")

    print()
    print(f"checked {len(targets)} named routes")
    print(f"server errors : {len(failures)}")
    print(f"redirects     : {len(redirects)}  (expected for auth-gated or alias routes)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
