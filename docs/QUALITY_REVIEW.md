# Quality Review

An audit of this repository, followed by the fixes it produced. Written against the code, not
against intentions: every claim below was checked by running it, and each fixed defect has a
regression test named after it.

## How to verify this review

    .\.venv\Scripts\python.exe manage.py check
    .\.venv\Scripts\python.exe manage.py check --deploy      # with DEBUG=false and a real secret
    .\.venv\Scripts\python.exe -m pytest tests -q
    .\.venv\Scripts\python.exe -m ruff check .
    npm run build
    .\.venv\Scripts\python.exe scripts/render_all_pages.py   # renders every named route
    npm run test:e2e                                          # needs a running server

## Defects found and fixed

### Critical

**Every organization-scoped API endpoint returned 403 to token-authenticated callers.**
`CurrentOrganizationMiddleware` skips bearer requests on purpose â€” it runs before DRF authenticates
anyone â€” and relies on `DocumentedAPIView.initial()` to resolve the organization afterwards. The
sixteen router ViewSets inherited plain `viewsets.*` instead of `OrganizationViewSetMixin`, so that
call site was skipped, `request.organization` stayed `None`, and `HasOrganization` denied every
request. `DriverShiftViewSet` bypassed the mixin as well. `/api/v1/auth/token/` was issuing tokens
that worked on twelve endpoints and failed on all the rest.

*Fixed* by making `OrganizationViewSetMixin` inherit `DocumentedAPIView`, which is load-bearing
rather than cosmetic, and routing `DriverShiftViewSet` through the same mixin.
*Test:* `TestEveryEndpointIsReachableWithAJWT` parametrised over all sixteen list endpoints.

**The entire `route-plans` API was non-functional.** The dispatch action was defined as
`def dispatch`, which overrides `View.dispatch` â€” the method DRF calls to route every inbound
request. Every call on the ViewSet arrived at the action instead, with no `pk`, and raised
`AssertionError`. List, retrieve, validate, clone and dispatch were all 500.

*Fixed* by renaming the method and preserving the public contract through
`@action(url_path="dispatch", url_name="dispatch")`, so `/api/v1/route-plans/{pk}/dispatch/` and
its reverse name are unchanged.
*Test:* `TestRoutePlanViewSetIsReachable`, plus a registry sweep that fails if any ViewSet action
shadows a reserved `View` method.

**Redis being optional was documented but not implemented.** `docs/architecture.md` and the README
both promised that everything degrades without Redis. Three places did the opposite:

| Path | Behaviour without Redis |
| --- | --- |
| `Celery .delay()` | ~20s stall, then `RuntimeError` â€” the Redis result backend retried its reconnect 20Ã—. Every mutating endpoint 500'd. |
| SSE progress stream | `ConnectionError` propagated, so the endpoint 500'd and the browser reloaded every 4s. |
| `publish()` | Untimed socket connect, so each progress update blocked for the OS TCP timeout. A full optimization run became a chain of multi-second stalls. |
| `/health/ready/` | Returned 503, so an orchestrator would kill a container serving every page correctly. |

*Fixed* with `apps/common/dispatch.py` (queue, else run inline), an `unavailable` SSE event that the
page handles by switching to polling, connect timeouts on every Redis client, and a readiness probe
that requires only the database. Measured improvement on the geocode task: **5.78s â†’ 0.25s**.
*Tests:* `TestRedisIsOptional`, `TestDispatchFallsBackToInline`.

### High

**Depot scoping failed open.** `membership_depot_ids` returned `depot_ids or None`, and `None` meant
"no restriction" to every consumer. A membership whose depots had been cleared therefore read and
wrote every depot in the organization â€” the opposite of the intended default. The seeder always
assigned a depot, so the demo never exercised it.

*Fixed* by keeping `None` for "no membership" (rejected by the permission layer) and `[]` for a real
membership scoped to nothing, honoured as nothing.
*Test:* `TestDepotScopingFailsClosed`.

**The production login page advertised working credentials**, including `admin@example.com`, which
is seeded with `is_staff=True`. The block had no `DEBUG` gate. The same page also replaced every
authentication error with "The email or password is incorrect", discarding Django's real message.

*Fixed* by gating on `{% if debug %}` (and adding the context processor that supplies it) and
rendering `form.non_field_errors`.

**`DEBUG` defaulted to `true` and `SECRET_KEY` to a literal.** A deployment that forgot one
variable ran with the debug page, no HSTS, no SSL redirect, `testserver` in `ALLOWED_HOSTS`, and a
publicly known signing key â€” invisibly, until it was exploited.

*Fixed* by inverting both defaults and raising at startup when the placeholder key is used with
DEBUG off.

**`EncryptedTextField` returned `""` on a decryption failure.** Rotating `FIELD_ENCRYPTION_KEY` or
`SECRET_KEY` made every existing ciphertext unreadable, and the handler converted that into a blank
customer email, phone number or webhook secret with no log and no error.

*Fixed* by raising `DecryptionFailed` and logging the key source.
*Test:* `TestEncryptedTextField`.

**Three mutation paths skipped the audit log.** The generic versioned `update`/`destroy` mixin, and
exception resolution. Everything else recorded actor, before/after state, IP and request id.

*Fixed* in the mixin, so a new updatable resource cannot forget. `record_audit` now accepts explicit
`resource_type`/`resource_id`, because Django clears `instance.pk` on delete and the log would
otherwise record a deletion against `"None"`.

**The webhook pipeline was advertised but dead.** `emit_webhook_event` had no callers, so signing,
retry, HMAC verification and the integrations status board never executed.

*Fixed* by publishing `plan.dispatched`, `pod.captured` and `route.completed` on transaction commit
â€” not inside the transaction, which would announce work a rollback then erased.

**Webhook URLs were an SSRF vector.** A subscription URL is operator-supplied and the delivery task
POSTs to it with the response readable from `WebhookDelivery.response_body`, with no host
validation.

*Fixed* by validating in a shared module at save time and again immediately before each send: HTTPS
only, and every resolved address must be globally routable. Verified blocked: cloud metadata,
loopback, `localhost`, RFC1918, LAN, IPv6 loopback, unresolvable hosts.

### Frontend

| Defect | Consequence | Status |
| --- | --- | --- |
| `driver/stop.html` read `order.packages`, `order.weight_g`, `order.volume_l` | None exist on `Order`, so the whole "Load" panel was permanently unreachable â€” drivers never saw the cargo they were collecting | Renamed to the real fields |
| Every flash message rendered as `notice-success` | All eight `messages.error(...)` calls, including "Select at least one order", showed green with a checkmark | Severity now derived from `message.level_tag` |
| Integrations page was a hardcoded list with an empty view context | All six providers always showed a green "Configured" badge; nothing could ever fail | State derived from settings and the database |
| `.app-main { margin-left: 0 }` in the 1280â€“1379px media query | The rail's gutter is a `padding-left`, so 88px of dead space sat beside every page at that width | Reset the correct property |
| `.status-*` classes missing for `OPEN`, `ACKNOWLEDGED`, `RESOLVED`, `ON_ROUTE`, `SKIPPED`, `OFF_DUTY`, `PARTIALLY_COMPLETED`, `SUPERSEDED` | Those pills rendered unstyled â€” an operational state lost its colour coding | Palette completed, with a test-adjacent comment on which enums it covers |
| "Select all" checkbox was `checked disabled` with no handler | Looked like a control and did nothing | Wired to every `[name=orders]` box, with an indeterminate state |
| `settings/index.html` hardcoded "Active" for every depot | The view passes inactive depots through, so a decommissioned depot read as operational | Reflects `depot.active` |
| Depot and break rows in the driver timeline linked to `#` | Every non-delivery row looked tappable and jumped to the page top | Plain content; only delivery stops navigate |
| `hx-on` / `hx-vals="js:"` / `hx-trigger="[â€¦]"` reachable under a CSP without `unsafe-eval` | An uncaught `EvalError` would break the interaction | `htmx.config.allowEval = false`, so htmx degrades predictably |
| Reports had no create UI | The only call to action was a link into Swagger, which cannot express a depot or a date range | Real export form. Deliberately no report-type picker: `export_report_task` produces exactly one KPI summary, so offering three types would advertise capability that does not exist |
| 13 form controls had a `<label>` with no `for` and no matching `id` | `recipient_name` had no accessible name at all | All paired |
| `/admin/â€¦` links rendered to non-staff users | A dispatcher clicking "Edit in admin" hit a login page they cannot pass, then an OTP form | Gated on `is_staff` |

### Medium

**Pagination was absent.** Every list endpoint serialized its entire table â€” a denial-of-service
lever aimed at the client. Adding it introduced a second defect that the regression tests caught:
several models have no `Meta.ordering`, so paginating them let the database return rows in a
different sequence per page, duplicating and dropping rows. The paginator now applies a
primary-key tie-break, and `?page_size=` is capped at 200.

**No branded error pages.** Django's plain-text 404/500 rendered because no `handler404`/`handler500`
was registered. Four pages now exist (`400`, `403`, `404`, `500`), built on a standalone layout that
never dereferences `request.organization` â€” because a 500 may be raised from inside the very
context processor that would supply it.

**N+1 queries.** `OrderViewSet` lacked a prefetch on two reverse relations, so one page of orders
issued **105 queries**. `DriverTodayAPIView` â€” the most-polled endpoint in the product â€” issued three
extra per route. The live-map vehicle endpoint queried per vehicle, and a first fix made it transfer
every location ping ever recorded for the fleet; it now uses a correlated subquery.

    GET /api/v1/orders/   105 queries -> 11

**`{{ rows|length }}` forced full evaluation** of unbounded querysets purely to count them, on three
pages.

**A template with invalid syntax passed `manage.py check`, passed lint, passed the unit tests, then
500'd for a visitor.** `{{ value|truncatechars:120 }}` is Django filter syntax; the project is
Jinja2, where the colon is a parse error. Django compiles templates lazily and `check` renders
nothing. `tests/unit/test_templates_compile.py` now compiles all 33 templates on every run.

**Two dead subsystems.** `apps/common/state_machine.py` was 100% unreferenced while the quality
review claimed it was the single authority â€” real transitions were hand-rolled string comparisons.
`emit_webhook_event` was never called. Both are now live, and the transition tables validate
themselves against the model `TextChoices` at import time. That check immediately caught two genuine
gaps: `CANCELLED` had no entry for either `Order` or `Route`.

**Dead code removed:** `AddressForm`, `CanManageOperations`, `ActiveModel`, three unused serializers,
the `structlog` dependency, and six unreferenced MySQL/WSL setup scripts. `bootstrap_mysql.sql` in a
PostGIS-only project was actively misleading.

## Test suite

    125 passed, 1 skipped

- `tests/unit` â€” solver, providers, transition tables, template compilation
- `tests/integration` â€” organization lifecycle, PostGIS, driver tokens, stop types, seeder
  idempotency, and 48 regression tests covering every defect above
- `tests/e2e` â€” Playwright: console-error sweep, dispatcher and driver journeys, responsive matrix
  at 1920 / 1440 / 1024 / 768 / 390 / 375 px

## Remaining limitations

Honest rather than flattering.

1. **API and browser coverage is uneven.** The `route-plans` and `route-stops` mutation paths are
   exercised by hand and by the Playwright journey, not by isolated tests. The most valuable next
   investment is integration tests per ViewSet.

2. **No automated type checking.** `mypy` and `django-stubs` are installed but there is no
   configuration and it is not run, so nothing catches a signature drift. Adding
   `[tool.mypy]` plus a strict target on `apps/common` would be the cheapest meaningful next step.

3. **The demo database goes stale.** `seed_demo` anchors service dates to today, so a database
   seeded weeks ago renders empty dashboards. Re-seed, or schedule it.

4. **Inline task execution holds a request.** With no broker, `optimize_run_task` runs OR-Tools in
   the request thread, bounded by `SOLVER_TIME_LIMIT_SECONDS` but not by `CELERY_TASK_TIME_LIMIT`,
   which does not apply to eager execution. This is the deliberate cost of "Redis is optional"; a
   deployment that cares should run a worker.

5. **`SECURE_HSTS_PRELOAD` is unset.** `check --deploy` reports it. Submitting to the browser preload
   list is effectively irreversible, so it is a conscious decision rather than a default.

6. **The reporting surface is one report.** The export produces a KPI summary. On-time performance
   and exception breakdowns are natural next features, and the export task is where they belong.

7. **`output/` still holds a stale MySQL data directory** from before the PostGIS migration, roughly
   80 MB. It is gitignored and unreferenced; delete it when convenient.
