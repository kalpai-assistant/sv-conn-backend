# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FastAPI + SQLAlchemy 2.0 async backend for an on-demand home services marketplace (Urban Company-style). Phone/OTP auth, geo-based provider matching, booking state machine, Razorpay payments, FCM push, and WebSocket tracking.

## Commands

**Dependency management** uses `uv` (not pip):
```bash
uv sync                   # Install all deps (runtime + dev)
uv sync --no-dev --frozen # Production install
uv add <package>          # Add a new dependency
```

**Run & develop:**
```bash
uv run uvicorn app.main:app --reload     # Dev server (auto-reload)
docker-compose up -d postgres redis      # Infrastructure only
celery -A app.tasks.celery_app worker --loglevel=info   # Task worker
celery -A app.tasks.celery_app beat --loglevel=info     # Scheduler
```

**Database:**
```bash
uv run alembic revision --autogenerate -m "message"
uv run alembic upgrade head
uv run python scripts/create_admin.py +91XXXXXXXXXX
```

**Seed ritual (SVC-312).** Run in this exact order against a freshly-migrated DB. Each step is idempotent — re-runs are safe and never wipe categories.

```bash
uv run alembic upgrade head                             # creates the 7 L1 tiles
uv run python scripts/seed_categories.py                # re-asserts hierarchy + parent_id links
uv run python scripts/seed_services.py                  # curated service names
uv run python scripts/seed_pricing_from_xlsx.py         # pricing + variants only, no category churn
```

`seed_pricing_from_xlsx.py` **refuses to run** unless the SVC-286 hierarchy (17 L2 slugs + `miscellaneous`) is already present — this is the guardrail added in SVC-312 so a re-run can never wipe the L1 tiles the way it did on 2026-09-23. The single source of truth for the tile list + L2→L1 mapping is `app/seed_data/l1_hierarchy.py`; both the migration and `seed_categories.py` import from there.

**Lint & format** — Ruff does both. CI enforces both blocking:
```bash
uv run ruff check .          # lint (E, W, F, I, B, UP rulesets)
uv run ruff check --fix .    # + auto-fix what's safe
uv run ruff format .         # apply formatter
uv run ruff format --check . # CI enforcement
```

**Pre-commit** — one-time install for auto-format + secret scan on commit:
```bash
pipx install pre-commit
pre-commit install                # writes .git/hooks/pre-commit
pre-commit run --all-files        # sanity check
```

**Tests:**
```bash
uv run pytest tests/ -v
uv run pytest tests/path/to/test_file.py::test_name -v  # Single test
uv run pytest tests/test_health.py tests/test_e2e_bypass.py \
    tests/test_logging_correlation.py tests/test_log_redaction.py \
    tests/test_celery_log_correlation.py tests/test_sentry_tagging.py \
    tests/test_cors_config.py  # Standalone (no DB) — fast local sanity
```

**E2E smoke** — Playwright against staging (SC-52). Own npm project:
```bash
cd e2e && npm install
STAGING_API_URL=... E2E_TEST_PHONE=... E2E_TEST_OTP=... npm test
```

## Architecture

### Layer Responsibilities

- **`app/main.py`** — App factory, lifespan (Redis init/teardown), middleware stack, router registration, Sentry init (SVC-46)
- **`app/config.py`** — All config via Pydantic Settings (reads from `.env`)
- **`app/database.py`** — Async engine + `SyncSessionLocal` (Celery-only sync fallback), ORM `Base`
- **`app/dependencies.py`** — `get_db`, `get_redis`, `get_current_user`, `require_role` (RBAC factory). `get_current_user` also binds the user into the Sentry scope (SC-47).
- **`app/exceptions.py`** — Domain exceptions + FastAPI exception handler registration
- **`app/logging_config.py`** — Structlog + JSON output + `request_id` / `task_id` contextvar processors + secret redaction (SC-46, SC-47, SC-50)
- **`app/routers/`** — Route handlers only; no DB logic, no business logic
- **`app/routers/health.py`** — `/health` (liveness, dumb 200) + `/healthz` (readiness, pings DB + Redis with 2s timeout). Unprefixed so LBs don't need to know the API version. (SC-48)
- **`app/services/`** — All business logic; services receive `AsyncSession` via DI
- **`app/models/`** — SQLAlchemy ORM models (9 tables)
- **`app/schemas/`** — Pydantic v2 request/response schemas
- **`app/tasks/`** — Celery async tasks (notifications, payments, cleanup). `log_correlation.py` binds `task_id` + inherited `request_id` into every worker log record via Celery signals (SC-46 followup).
- **`app/utils/`** — Thin wrappers for external clients (SMS, FCM, Razorpay, S3, geo)
- **`app/middleware/`** — Request ID correlation (also tags Sentry, SC-47), JWT extraction, Redis rate limiting, admin IP allowlist (SC-75)

### Runbooks (top-level)

Every operational concern has its own runbook — followable by anyone with AWS console access, no institutional knowledge required:

- **[INFRA.md](INFRA.md)** — provisioning topology (VPC, RDS, ElastiCache, Fargate, SSM). SC-44.
- **[BACKUPS.md](BACKUPS.md)** — RDS backup config + quarterly restore drill. SC-34.
- **[SECRETS.md](SECRETS.md)** — every secret enumerated, rotation cadence + procedure, incident response (DPDP 72h clock). SC-50.
- **[OBSERVABILITY.md](OBSERVABILITY.md)** — Better Stack Logs vendor decision, alert catalog (P1/P2/P3), dashboard spec, Sentry↔logs correlation. SC-47.
- **[ADMIN.md](ADMIN.md)** — admin IP allowlist + role hardening. SC-75.
- **[PURE_CODE_QUEUE.md](PURE_CODE_QUEUE.md)** — in-flight pure-code work queue. Delete when drained.

### Booking State Machine

Transitions are enforced by `VALID_TRANSITIONS` dict in `app/models/booking.py`. Every state change calls `_log_transition` which writes to `booking_status_logs`. Valid flow:

```
requested → accepted → en_route → in_progress → completed
                     ↘                         ↗
                      cancelled (from most states)
```

Doorstep OTP (4-digit, stored in Redis) is required to transition from `en_route` → `in_progress`.

### Auth Flow

1. `POST /send-otp` — generates 6-digit OTP, stores in Redis with 10-min TTL, sends via SMS
2. `POST /verify-otp` — validates OTP, upserts user, returns JWT access + refresh tokens
3. `POST /refresh` — exchanges refresh token for new token pair
4. JWT middleware (`app/middleware/auth.py`) attaches user to request context; `get_current_user` dependency enforces authentication

Rate limiting: 5 OTP attempts per phone per hour, tracked in Redis.

**E2E bypass (SC-52):** in staging, a whitelisted phone + fixed OTP skips the SMS path. Hard-refuses when `ENVIRONMENT=production`. See `AuthService._is_e2e_test_phone`.

### Observability (SC-46, SC-47, SC-50)

- **Structured logs** — `LOG_JSON=true` in staging/prod emits newline-delimited JSON. Every record carries `timestamp`, `level`, `logger`, `event`, plus `request_id` / `task_id` when in scope.
- **Request ID correlation** — `RequestIDMiddleware` binds a contextvar; structlog processor adds it to every record. Propagates into Celery workers via `task_prerun` signal.
- **Secret redaction** — structlog processor scrubs sensitive keys (`password`, `otp`, `token`, `secret`, `razorpay_signature`, ...) and known patterns (JWT, `rzp_live_*`, `AKIA*`) before render. Safety net for accidental logging.
- **Sentry** — no-op when `SENTRY_DSN` is unset (dev-safe). `send_default_pii=False` — never ship JWT/phone/email to Sentry. Request-scoped tag + user context wired from middleware + `get_current_user`.

### Key Design Patterns

- **RBAC**: `require_role("admin")` / `require_role("provider")` is a dependency factory — returns a FastAPI dependency
- **SMS abstraction**: `AbstractSMSClient` → `MSG91Client` / `TwilioClient`; swap via config without touching callers
- **Commission split**: 80/20 (provider/platform) calculated at booking creation in `booking_service.py`
- **Geo queries**: PostGIS-backed; `app/utils/geo.py` builds WKT geometries; nearby provider search uses `ST_Distance`
- **PII encryption**: AES-256 via `app/utils/encryption.py` for sensitive fields at rest
- **WebSocket auth**: JWT passed as `?token=<jwt>` query param, validated per connection in `app/routers/tracking.py`
- **Celery + async DB**: Workers use `SyncSessionLocal` (derived from async URL) since Celery is sync
- **CORS**: `CORS_ALLOWED_ORIGINS` env var, comma-separated. Dev default in `config.py` covers localhost + Capacitor + prod web. Wildcard deliberately not accepted (spec violation with `allow_credentials=True`).

### Infrastructure (docker-compose)

5 services: `api`, `celery-worker`, `celery-beat`, `postgres` (PostGIS image), `redis`. Postgres requires the PostGIS extension — do not use a plain postgres image.

### Beta DB (RDS, SVC-304)

Beta backend runs on App Runner (`eazyserv-backend` + `eazyserv-worker`) in `ap-south-1`. Postgres moved from Neon to AWS RDS on 2026-09-18 after Neon's free-tier compute quota was exhausted (see SVC-304).

- **Instance:** `eazyserv-beta-postgres` (PostgreSQL 16.15, db.t3.micro, gp3 20 GB, single-AZ)
- **Endpoint:** `eazyserv-beta-postgres.cruawyqc2v1u.ap-south-1.rds.amazonaws.com:5432`
- **DB / user:** `eazyserv` / `eazyserv_admin`
- **Master password:** SSM SecureString `/eazyserv/beta/RDS_MASTER_PASSWORD`
- **Connection URLs:** SSM `/eazyserv/beta/DATABASE_URL` (asyncpg, `?ssl=require`) and `/eazyserv/beta/SYNC_DATABASE_URL` (psycopg2, `?sslmode=require`)
- **Networking:** publicly accessible, SG `eazyserv-beta-rds-sg` opens `5432/tcp` from `0.0.0.0/0`. Temporary — tightened via VPC Connector in the SVC-304 follow-up.
- **Guardrails:** `DeletionProtection=True`, `StorageEncrypted=True`, `rds.force_ssl=1` (parameter group `eazyserv-beta-postgres16`), 7-day PITR, auto-minor-version-upgrade on.

psql from a workstation:

```bash
export AWS_PROFILE=eazyserv AWS_DEFAULT_REGION=ap-south-1
export PGPASSWORD=$(aws ssm get-parameter --name /eazyserv/beta/RDS_MASTER_PASSWORD \
  --with-decryption --query 'Parameter.Value' --output text)
psql -h eazyserv-beta-postgres.cruawyqc2v1u.ap-south-1.rds.amazonaws.com \
     -U eazyserv_admin -d eazyserv
```

Run `alembic upgrade head` from `backend/` with `DATABASE_URL` + `SYNC_DATABASE_URL` set from the SSM params above. Deploys no longer auto-migrate (Neon shadow-branch path removed); `alembic check` in ci.yml catches missing migrations at PR time.

## Environment

Copy `.env.example` to `.env`. Key variable groups: JWT secrets, OTP config, SMS provider (MSG91/Twilio), Razorpay keys, Firebase credentials, S3/MinIO, Google Maps API, AES encryption key, CORS origins, E2E bypass (staging only), observability (Sentry DSN, LOG_JSON, LOG_LEVEL, ENVIRONMENT).

## Testing

Tests use `pytest-asyncio` with `AsyncClient` over ASGI transport. `conftest.py` provides fixtures: `db_session`, `client`, `redis_client`, `auth_headers`. Factories from `factory-boy` generate test data.

**Standalone tests** (no DB / no Redis) run in ~1s and cover the observability, health, e2e-bypass, CORS, and Sentry-tagging surfaces. Use them for fast local sanity when iterating on infra code.

## CI

`.github/workflows/ci.yml` runs on push to main + every PR:
1. `ruff check` (lint) + `ruff format --check` (format enforcement) — blocking
2. PostGIS extension enable → `alembic upgrade head` → `alembic check` (drift catch) — blocking
3. `pytest tests/` — blocking

`.github/workflows/e2e-nightly.yml` runs the Playwright suite against staging at 02:00 IST (SC-52).
