# Architecture

The application is a Django monolith with domain-isolated apps and asynchronous workers.

## Request and planning flow

1. Session-authenticated operations pages and DRF endpoints apply organization/depot scope.
2. Orders enter through web, API, or CSV import and must have validated WGS84 coordinates.
3. A planning run snapshots eligible orders, fleet, drivers, shifts, skills, and profile weights.
4. The routing adapter writes a compressed distance/time matrix through Django storage.
5. OR-Tools creates a feasible assignment using integer meter/second costs and hard constraints.
6. Results are persisted atomically as a versioned route plan with unassigned explanations.
7. Validation and manual edits create new draft versions; dispatched plans are immutable.
8. Driver actions create idempotent events and advance stop/order/route state under row locks.

## Storage boundaries

- PostgreSQL with PostGIS stores operational and WGS84 geometry data. Geometry columns are SRID
  4326 and GiST-indexed; the PostGIS extension is enabled by migration.
- Redis carries Celery work, cache entries, throttles, and SSE publications. It is genuinely
  optional, and each consumer degrades in its own way rather than failing:
  - *cache* falls back to in-process memory outside DEBUG; rate limiting deliberately degrades
    **open**, so a cache outage allows traffic instead of rejecting it;
  - *task dispatch* runs the task inline in the request (`apps.common.dispatch.dispatch`), so an
    accepted write is never lost because no broker was listening;
  - *SSE* reports an `unavailable` event and the progress page falls back to polling the same
    endpoint with `Accept: application/json`;
  - *readiness* returns `200` and lists the broker under `degraded`, because killing a container
    that is serving every page correctly is the wrong trade.

  A missing broker previously caused a ~20 second stall and then a `RuntimeError` on every
  mutating endpoint, because the Redis result backend retried its reconnect twenty times before
  giving up. `tests/integration/test_audit_regressions.py` pins this behaviour.
- Django storage holds private POD, imports, matrices, and exports. The local backend is the
  filesystem; the interface is compatible with an S3-backed production implementation.

## API contract

`/api/v1` is resource-oriented. Successful responses return the serialized resource with the
conventional status code (`200`, `201` plus `Location`, or `204` for deletions) rather than being
wrapped in an envelope, so responses stay directly usable by the browsable API, the generated
OpenAPI schema, conditional requests and caching.

Errors are normalised by `apps.common.api.exception_handler` into one document shape:

    {"code": "...", "message": "...", "field_errors": {...}, "request_id": "..."}

`request_id` is the same value as the `X-Request-ID` response header and the structured log entry,
which is what makes a user-reported failure traceable to a single request.

Mutating endpoints accept an `Idempotency-Key` header. The first request's status code and body are
stored against the key and replayed verbatim for retries, so a client that times out can safely
resend without duplicating the operation.

List endpoints are paginated by `apps.common.api.OrganizationPageNumberPagination`:

    {"count": N, "page": 1, "page_size": 50, "total_pages": N, "next": ..., "previous": ..., "results": [...]}

Two details matter. `?page_size=` is capped at 200, because without a ceiling a caller can request an
entire table in one response — a denial-of-service lever aimed at the client rather than at us. And
the paginator applies a primary-key tie-break to any queryset without a declared ordering, because
paginating an unordered queryset lets the database return rows in a different sequence per page,
so a client walking the pages sees duplicates and misses rows.

## Provider boundaries

The routing provider module defines geocoder and router interfaces. Fake providers are
deterministic and internet-independent. Nominatim and OSRM adapters use configured base URLs and
fail explicitly; the system never silently changes routing semantics.

Webhook targets are validated in `apps/integrations/validation.py` at save time *and* again
immediately before each delivery. Only `https` URLs resolving to public addresses are accepted:
subscription URLs are operator-supplied and the delivery task performs an outbound POST, so an
unvalidated URL could be aimed at cloud instance metadata, the loopback interface or an internal
service, with the response read back out of `WebhookDelivery.response_body`. Re-validating at send
time is necessary because DNS can be re-pointed after the subscription was saved.

## Security model

- Email/password session authentication for internal users.
- JWT access and revocable driver devices for mobile APIs.
- Organization membership and depot scope on operational querysets.
- PII encryption at rest, private authorized file delivery, request IDs, webhook signatures, and
  audit events for critical changes.

### Where the boundaries are enforced

**Organization resolution runs twice, deliberately.** `CurrentOrganizationMiddleware` skips bearer
requests because middleware executes before DRF has authenticated anyone, leaving the organization
unresolved. `DocumentedAPIView.initial()` finishes the job after DRF authentication. Every
organization-scoped ViewSet must inherit `OrganizationViewSetMixin`, which inherits
`DocumentedAPIView` — when the router ViewSets inherited plain `viewsets.*` instead, this call site
was skipped and `HasOrganization` denied all sixteen of them with 403.

A regression test walks the router registry and asserts that no ViewSet action shadows a reserved
`View` method. The `route-plans` endpoint had an action literally named `dispatch`, which overrode
`View.dispatch` — the method DRF calls to route every inbound request — and made the entire
ViewSet, including its list endpoint, raise `AssertionError`.

**Depot scoping is fail-closed.** `apps/common/scoping.py` distinguishes "no membership" (`None`,
rejected by the permission layer) from "membership scoped to zero depots" (`[]`, honoured as
nothing). Collapsing the two with `depot_ids or None` made a depot-less member see — and write to —
every depot in the organization.

**The transition table is validated against the models.** `apps/common/state_machine.py` holds every
legal status change and asserts at import time that each table's keys and values are values the
corresponding model actually defines, and that no status is missing. It caught two real gaps on
first run (`CANCELLED` for both `Order` and `Route`).

**Encrypted columns fail loudly.** `EncryptedTextField` raises `DecryptionFailed` when a value
cannot be decrypted rather than returning `""`. Returning an empty string turned a key rotation into
a silent, permanent wipe of every customer email, phone number and webhook secret, with no log line
to indicate anything had gone wrong.

## Observability

`/health/live/` is liveness. `/health/ready/` is readiness and requires only the database; cache and
broker are reported under `degraded` so a platform does not restart a healthy container.
`/metrics/` exposes Prometheus counters. Structured logging runs through stdlib `logging` with a
request-id filter, and every log line and `X-Request-ID` header carries the same correlation id.

Sentry is initialised only when `SENTRY_DSN` is set, so a copied `.env` cannot ship local stack
traces to a third party's project, and request bodies are excluded because they carry customer
addresses and driver PII.


