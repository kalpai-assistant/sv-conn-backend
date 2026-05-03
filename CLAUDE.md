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
uv run python scripts/seed_categories.py
uv run python scripts/create_admin.py +91XXXXXXXXXX
```

**Tests:**
```bash
uv run pytest tests/ -v
uv run pytest tests/path/to/test_file.py::test_name -v  # Single test
```

## Architecture

### Layer Responsibilities

- **`app/main.py`** — App factory, lifespan (Redis init/teardown), middleware stack, router registration
- **`app/config.py`** — All config via Pydantic Settings (reads from `.env`)
- **`app/database.py`** — Async engine + `SyncSessionLocal` (Celery-only sync fallback), ORM `Base`
- **`app/dependencies.py`** — `get_db`, `get_redis`, `get_current_user`, `require_role` (RBAC factory)
- **`app/exceptions.py`** — Domain exceptions + FastAPI exception handler registration
- **`app/routers/`** — Route handlers only; no DB logic, no business logic
- **`app/services/`** — All business logic; services receive `AsyncSession` via DI
- **`app/models/`** — SQLAlchemy ORM models (9 tables)
- **`app/schemas/`** — Pydantic v2 request/response schemas
- **`app/tasks/`** — Celery async tasks (notifications, payments, cleanup)
- **`app/utils/`** — Thin wrappers for external clients (SMS, FCM, Razorpay, S3, geo)
- **`app/middleware/`** — Request ID correlation, JWT extraction, Redis rate limiting

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

### Key Design Patterns

- **RBAC**: `require_role("admin")` / `require_role("provider")` is a dependency factory — returns a FastAPI dependency
- **SMS abstraction**: `AbstractSMSClient` → `MSG91Client` / `TwilioClient`; swap via config without touching callers
- **Commission split**: 80/20 (provider/platform) calculated at booking creation in `booking_service.py`
- **Geo queries**: PostGIS-backed; `app/utils/geo.py` builds WKT geometries; nearby provider search uses `ST_Distance`
- **PII encryption**: AES-256 via `app/utils/encryption.py` for sensitive fields at rest
- **WebSocket auth**: JWT passed as `?token=<jwt>` query param, validated per connection in `app/routers/tracking.py`
- **Celery + async DB**: Workers use `SyncSessionLocal` (derived from async URL) since Celery is sync

### Infrastructure (docker-compose)

5 services: `api`, `celery-worker`, `celery-beat`, `postgres` (PostGIS image), `redis`. Postgres requires the PostGIS extension — do not use a plain postgres image.

## Environment

Copy `.env.example` to `.env`. Key variable groups: JWT secrets, OTP config, SMS provider (MSG91/Twilio), Razorpay keys, Firebase credentials, S3/MinIO, Google Maps API, AES encryption key.

## Testing

Tests use `pytest-asyncio` with `AsyncClient` over ASGI transport. `conftest.py` provides fixtures: `engine`, `db_session`, `client`, `auth_headers`. Factories from `factory-boy` generate test data.
