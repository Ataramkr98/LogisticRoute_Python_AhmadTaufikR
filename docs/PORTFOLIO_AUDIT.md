# Portfolio Audit — RouteOps

A file-by-file sweep of the repository, what condition each part was in, and what changed. Kept
separate from [`QUALITY_REVIEW.md`](QUALITY_REVIEW.md), which covers defects and fixes in depth;
this document is the structural map.

## Frontend

| Folder | Purpose | Condition found | Changes |
| --- | --- | --- | --- |
| `templates_jinja2/layouts/` | `app` (operations shell), `driver` (field shell), `error` (new) | Complete and well-structured. Error messages ignored severity. `.ti` chevron overrode its own size utility. | Flash severity component; added the standalone error layout |
| `templates_jinja2/components/` | Jinja macros: icons, empty state, badges, flash | Icons and badges exemplary — a single Tabler family at consistent sizes, `aria-hidden` throughout | Added `flash_messages` |
| `templates_jinja2/dashboard/` | Command Center | Claimed a chart and activity feed in the docs; **neither existed** while `chart.js` sat unused in `package.json` | Real 7-day throughput chart from actual rows, plus an audit-log activity feed. Chart.js tree-shaken to the six components used |
| `templates_jinja2/orders/` | List, detail, create, CSV import | Load-bearing. Driver-stop panel referenced three nonexistent `Order` fields. Form bypassed the design system. `as_p()\|safe` disabled autoescape. | Field names corrected; `.field` widget classes; `\|safe` removed |
| `templates_jinja2/planning/` | Run setup, progress, review | "Select all" was a disabled checkbox wired to nothing. Drag hint shown when dragging was disabled. Two buttons had no `type`. Progress phase labels had no data hook and never updated. | Select-all implemented; conditional hint; `type` attributes; live phase labels driven from `payload.percent` |
| `templates_jinja2/routes/`, `live_map/` | Route list and detail, live map | Sound. Map was re-initialised only on first load. | `setupLiveMap` extracted and re-bound on `htmx:afterSwap` |
| `templates_jinja2/fleet/` | Drivers, vehicles, depots | Depot status hardcoded "Active". Admin links ungated. Row counts forced full evaluation. | Status reflects `active`; links gated; `rows_count` computed in SQL |
| `templates_jinja2/driver/` | Today, route, stop, POD, history | Depot/break rows linked to `#`. POD had no associated labels and never showed a saved proof. | Non-delivery rows are plain content; all labels paired; saved-proof confirmation added |
| `templates_jinja2/exceptions/` | Exception queue and resolution | Status pills undefined for `OPEN`/`ACKNOWLEDGED`. Resolution label unassociated. | Palette completed; label paired |
| `templates_jinja2/reports/` | Export history | **No way to create an export.** The only call to action was a link into Swagger that could not carry a date range or depot | Real form posting to a new `reports_page` POST branch |
| `templates_jinja2/settings/` | Planning profiles, depots | Every depot hardcoded "Active". Admin links ungated. | Correct status; links gated; readable for non-staff |
| `templates_jinja2/integrations/` | Capability status board | Entirely hardcoded in the template with an **empty view context**; all six providers always green | Rows and states derived from settings and the database |
| `templates_jinja2/registration/` | Unusable-account notice | Renders an empty bordered block for anonymous visitors | Left as-is; harmless |
| `assets/css/app.css` | Design system | Strong: 16 coherent tokens, consistent 8px radius, consistent status/metric/table/notice primitives. **No radius token.** Dead `.btn`, `.btn-danger`, `.notice-info` rules. 88px gutter bug. Incomplete status palette | Radius tokens; dead rules removed; gutter fixed; palette completed |
| `assets/js/app.js` | htmx, Alpine, Leaflet, Sortable | No `htmx.config.allowEval` despite a CSP with no `unsafe-eval`. No re-init after htmx swap. Hardcoded URL. Duplicated progress paint logic. `chart.js` never imported | `allowEval=false`; afterSwap re-init; URL from `data-*`; shared paint; chart added |
| `static/build/` | Compiled bundle | 346 kB → 514 kB, of which Chart.js is ~168 kB | Selective Chart.js registration instead of `chart.js/auto` |

## Backend

| Module | Condition found | Changes |
| --- | --- | --- |
| `config/settings.py` | Insecure defaults (`DEBUG=true`, literal secret key); `CACHES` never wired to `REDIS_URL`; `CELERY_TASK_ALWAYS_EAGER` never read though set in `.env`; hardcoded console email; `sentry-sdk` a dependency but never initialised | All four corrected; email configurable; Sentry wired, opt-in, PII excluded |
| `config/urls.py` | No error handlers | `handler400/403/404/500` registered |
| `apps/common/scoping.py` | **Failed open** on an empty depot set | Fail-closed |
| `apps/common/permissions.py` | Sound; `CanManageOperations` unreferenced | Dead code removed |
| `apps/common/middleware.py` | Sound by design; correctly skips bearer requests, which is why the ViewSet mixin mattered | Unchanged |
| `apps/common/api_views.py` | 16 ViewSets unreachable with a JWT; `route-plans` fully broken; missing audits on three paths; N+1s | All fixed; `OrganizationViewSetMixin` made load-bearing |
| `apps/common/serializers.py` | Sound, and `is_superuser` correctly appears only in the custom user manager and the Django admin, never in a serializer. Three serializers unreferenced | Unused serializers removed |
| `apps/common/forms.py` | No widget classes; `AddressForm` unused | Widget classes; dead code removed; `ReportExportForm` added |
| `apps/common/state_machine.py` | **100% dead** while documented as the authority | Rewritten to validate itself against the models, and wired into every real transition |
| `apps/common/fields.py` | Silent `""` on decryption failure | Raises `DecryptionFailed` |
| `apps/common/events.py` | SSE raised without Redis; `publish` untimed | Graceful degradation; connect timeouts |
| `apps/common/dispatch.py` | **Did not exist** | New: queue-or-run-inline dispatch |
| `apps/common/error_views.py` | **Did not exist** | New: four branded handlers |
| `apps/integrations/` | `emit_webhook_event` never called; SSRF surface; import cycle | Wired; `validation.py` extracted; cycle broken |
| `apps/reports/` | No create path from the UI | `reports_page` accepts POST |
| `apps/tracking/services.py` | Hand-rolled transition checks; no webhook publication | Routed through the state machine; publishes on commit |
| Other app modules | Services and tasks consistent, audited throughout, all mutations recording audit rows | Additions only |

## Other

| Path | Condition found | Changes |
| --- | --- | --- |
| `scripts/` | Six unreferenced MySQL/WSL scripts in a PostGIS-only project | Removed. Added `render_all_pages.py` and a management command for the shared-Postgres test-DB problem. `capture-portfolio.cjs` now writes to `docs/screenshots/` |
| `tests/` | 5 files, no regression coverage for any real defect; no template-compilation guard | +2 files, 48 regression tests, 33-template compile guard |
| `docs/` | Two review documents asserting claims that were false — `state_machine.py` as the single authority, no legacy scripts left, a chart that did not exist | Rewritten to verified reality; `architecture.md` extended |
| `.env.example` | Documented `OTEL_EXPORTER_OTLP_ENDPOINT` with no OpenTelemetry package installed; omitted email, eager mode, API page size | Corrected and expanded |
| `.github/workflows/ci.yml` | Sound | Unchanged |
| `README.md` | Good, but silent on pagination, deployment, screenshots, and the shared-Postgres test trap | Rewritten with a deployment guide and a corrected Redis section |
| `.env` | Contains a **live database credential** | Not modified. Rotate before publishing this directory anywhere |
| `output/` | Holds a stale ~80 MB MySQL data directory and prior screenshots | Gitignored. Screenshots moved to `docs/screenshots/` and the directory left for the owner to delete |
