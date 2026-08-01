# Pure-Code Queue

Everything below is **doable without external accounts, credentials, or
vendor UI clicks**. Cross-cuts backend + frontend. Written 2026-07-30 so
tomorrow's session can pick up cold without re-deriving the list.

**Housekeeping:** delete this file once the queue is drained. Update the
"status" column as items ship (or check off inline). A "Done" section at
the bottom is deliberately kept short — recent context only, everything
older is in git history.

---

## Legend

- 🟢 **P0** — next up, high-leverage
- 🟡 **P1** — worth doing, batch alongside P0
- 🔵 **P2** — nice-to-have, only if the P0/P1s clear
- ✅ **Done**
- ⚠️  **Depends on another item** (dependency called out inline)

---

## Backend

| ID | Prio | Item | Effort | Why |
|---|---|---|---|---|
| **B1** | 🟢 P0 | **Ruff `format` sweep + `format --check` in CI** | 30m | 72-file reformat in one commit. Closes the item I deferred so it wouldn't collide with in-flight PRs. Prevents future style-drift PR noise. |
| **B2** | 🟡 P1 | **`get_redis` → `app.state.redis` + lifespan refactor** | 1h | Kills the per-test fixture workaround from PR #35, removes the module global. Proper Redis `.aclose()` in prod shutdown. |
| **B3** | 🟡 P1 | **`app/utils/currency.py` — paise/rupees Decimal helper** | 45m | Precondition for SVC-150 (Float rounding I flagged in PR #30). Doesn't require the migration — just the helper + tests, so PR #29/#30 have something to standardize on when they cut over. |
| **B4** | 🟢 P0 | **CORS origins to config** | 20m | Currently hardcoded in `app/main.py`. Env-driven is table-stakes for staging vs prod. |
| **B5** | 🟡 P1 | **`X-RateLimit-*` + `Retry-After` response headers** | 30m | Frontend can back off proactively instead of retry-and-fail. |
| **B6** | 🟡 P1 | **Idempotency-key support on `POST /bookings/`** | 1h | Prevents duplicate bookings on network retry. Redis-backed (`idem:{user_id}:{key}` → response body cached ~15 min). |
| **B7** | 🟢 P0 | **API contract tests against OpenAPI spec** | 1h | Assert every declared schema matches actual response. Would have caught the `booking_id` vs `bookingId` bug fixed in the frontend types PR. |
| **B8** | 🔵 P2 | **Celery health check** | 30m | `/healthz` currently pings DB+Redis only. Add "any worker responded to ping in last 30s" probe. Requires a Redis heartbeat key set by workers. |
| **B9** | 🔵 P2 | **JSON-schema tightening on Razorpay webhook payloads** | 30m | A few `dict[str, Any]` slots could be typed via Pydantic. |
| **B10** | 🟢 P0 | **Backend `CLAUDE.md` refresh** | 15m | Add ruff/pre-commit/redaction/`OBSERVABILITY.md`/Playwright dir. Doc-only. |
| **B11** | 🔵 P2 | **Consolidate `_create_booking_with_deps` fixture** | 45m | Duplicated across 3 test files. Move to `tests/conftest.py`. |

## Frontend

| ID | Prio | Item | Effort | Why |
|---|---|---|---|---|
| **F1** | 🟢 P0 | **`Sentry.setUser` in AuthContext** | 15m | Login → `setUser({id, phone})`; logout → `setUser(null)`. Sentry filterable by user. Small, high-value, mirrors what B7 does backend-side (SC-47 shipped). |
| **F2** | 🟡 P1 | **`apiFetch` retry on 5xx / network error** with exponential backoff (max 2 attempts, GET only) | 45m | Bangalore mobile networks flake. Idempotent GET only — non-GET stays as-is. |
| **F3** | 🔵 P2 | **Offline detection banner** | 45m | `navigator.onLine` + `online`/`offline` events → toast "You're offline". |
| **F4** | 🔵 P2 | **Loading skeletons for high-traffic screens** (home, service detail, bookings list) | 1h | Perceived-perf win. Users see structure instantly instead of a spinner. |
| **F5** | 🟢 P0 | **Error toast standardization** | 30m | Multiple screens roll their own; extract `showApiError(err)` in `lib/toast.ts`. |
| **F6** | 🔵 P2 | **Feature-flag helper** (localStorage-backed) | 30m | Cheap way to gate risky bits during beta rollout. Later swap for LaunchDarkly / GrowthBook if scale warrants. |
| **F7** | 🟡 P1 | **Bundle-size analysis + code-split admin routes** | 45m | Admin bundle currently ~500 KB — no consumer needs it. TanStack lazy routes. |
| **F8** | 🟢 P0 | **Frontend `CLAUDE.md` refresh** | 15m | Note api-types codegen, pre-commit, ESLint rule promotions, `types/razorpay.d.ts`. |

## Cross-cutting docs

| ID | Prio | Item | Effort | Why |
|---|---|---|---|---|
| **D1** | 🟡 P1 | **`CONTRIBUTING.md`** — clone → run → first PR in 5 min | 30m | Onboarding for future contributors. |
| **D2** | 🟢 P0 | **Rollback runbook** (one page) | 20m | "Deploy broke prod, what now?" — Fargate task-def rollback, alembic downgrade path. Referenced from OBSERVABILITY.md alerts. |
| **D3** | 🔵 P2 | **On-call playbook stub** | 30m | Maps each P1/P2 alert (from OBSERVABILITY.md) to a "first 5 min" checklist. |

---

## Recommended execution order

Batch by area so review stays scoped and PRs are small.

### Batch A — backend hygiene (~1h 5m)
Ship first — B1 unblocks nothing but simplifies every subsequent PR.
- [ ] **B1** ruff format sweep
- [ ] **B4** CORS to config
- [ ] **B10** backend CLAUDE.md refresh

### Batch B — backend defensive polish (~2h 30m)
Real user-facing value.
- [ ] **B7** API contract tests ← highest ROI in the batch
- [ ] **B5** rate-limit headers
- [ ] **B6** idempotency key on POST bookings

### Batch C — frontend polish (~1h 45m)
- [ ] **F1** Sentry setUser
- [ ] **F5** error toast standardization
- [ ] **F8** frontend CLAUDE.md refresh
- [ ] **F2** apiFetch retry

### Batch D — docs (~50m)
Can do inline with any batch, or standalone.
- [ ] **D2** rollback runbook
- [ ] **D1** CONTRIBUTING.md

### Deferred until we hit an actual pain
- B2, B8, B9, B11, F3, F4, F6, F7, D3

**Total P0+P1 work:** ~7 hours across 3-4 PRs. One focused session
should clear P0 (batches A + start of B); the rest fits in a second
session.

---

## Not on this list (deliberately)

**External-blocker items** — do NOT confuse these with pure-code:

- **SC-45b** — deploy pipeline (needs AWS account)
- **SC-49** — Razorpay live + FCM prod (needs KYC approval + FCM setup)
- **SC-58 through SC-66** — Play + App Store distribution (needs
  console accounts)
- **SC-74** — APNs `.p8` (needs Apple Dev org active)
- **SC-47 external half** — Better Stack account provisioning, ingest
  token, dashboard build-out (the *code + runbook* half shipped;
  see OBSERVABILITY.md for the checklist)

**Junior queue** — PR #29 / #30 (SVC-143 / SVC-144) coordination bug
fixes I flagged in the reviews. Their queue unless you want me to
override.

**Speculative / over-scope for beta:**
- mypy / pyright — big win, but likely lots of preexisting issues; risk
  of derailing the beta window.
- OpenTelemetry APM tracing — Sentry's `browserTracingIntegration`
  covers enough for now.
- Real-user metrics — Sentry has a beta; skip until users complain
  about a specific screen.

---

## Recently done (context; delete after two weeks)

Shipped between 2026-07-28 and 2026-07-30, may be referenced by items
above:

- **SC-45a** — Backend + Frontend CI (ruff, alembic check blocking,
  Playwright build, npm ci)
- **SC-46** — structlog + Sentry + `request_id` correlation; Celery
  followup adding `task_id` + `worker_process_init` structlog config
- **SC-48** — `/health` + `/healthz` (DB + Redis pings)
- **SC-50** — Secrets rotation runbook + structlog secret redaction
  processor
- **SC-52** — Playwright E2E smoke + nightly cron + env-guarded OTP
  bypass
- **SC-47** — Log aggregation runbook (Better Stack vendor decision,
  alert catalog, Fargate task-def template) + Sentry `set_tag` /
  `set_user` correlation
- **Frontend api-types cleanup** — OpenAPI codegen, `no-explicit-any`
  promoted to `error`
- **Frontend ErrorBoundary + Sentry init**
- **Pre-commit hooks** — gitleaks + ruff/prettier in both repos
- **`get_redis` fixture fix** — per-test Redis client to sidestep the
  module-global event-loop leak (proper fix tracked as B2)
