# Eazyserv Backend — Build Walkthrough

## What Was Built

A complete **FastAPI + SQLAlchemy 2.0 async** backend for an on-demand home services marketplace (Urban Company clone), built from scratch per the LLD specification.

---

## Dependency Management

**Choice: `uv` + `pyproject.toml` instead of `requirements.txt`**

| | `requirements.txt` | `uv` + `pyproject.toml` |
|---|---|---|
| Resolution | Naïve | SAT-solver, conflict detection |
| Lock file | Manual `pip freeze` | Auto `uv.lock` (committed to git) |
| Install speed | Slow | 10–100× faster (Rust) |
| Dev deps | Separate file convention | First-class `[dependency-groups]` |
| Standard | Legacy | PEP 517/518/621 |

```bash
# Install everything (runtime + dev):
uv sync

# Install runtime only (Dockerfile):
uv sync --no-dev --frozen

# Add a new package:
uv add <package>
```

---

## File Structure Created

```
eazyserv/
├── app/
│   ├── main.py                    # App factory, lifespan, middleware stack
│   ├── config.py                  # Pydantic Settings (env-based)
│   ├── database.py                # Async engine + SyncSessionLocal for Celery
│   ├── dependencies.py            # get_db, get_redis, get_current_user, require_role
│   ├── exceptions.py              # Domain exceptions + handler registration
│   ├── middleware/                # RequestID, RateLimit, JWT attach
│   ├── models/                    # 9 ORM models (all aligned to HLD schema)
│   ├── schemas/                   # 7 Pydantic v2 schema modules
│   ├── services/                  # 9 service classes (all business logic)
│   ├── routers/                   # 9 routers → 41 routes
│   ├── tasks/                     # Celery app + 3 task modules
│   └── utils/                     # 8 utility modules
├── alembic/env.py                 # Async-aware Alembic
├── tests/                         # conftest + 3 test files + 2 factories
├── scripts/                       # seed_categories, create_admin
├── pyproject.toml                 # uv-managed deps
├── docker-compose.yml             # PostGIS + Redis with healthchecks
└── .env.example
```

---

## Key Design Decisions

### SOLID Adherence

| Principle | Where Applied |
|-----------|---------------|
| **S** — Single Responsibility | Each service owns one domain (AuthService ≠ BookingService). Routers only call services; no DB logic in routers. |
| **O** — Open/Closed | `AbstractSMSClient` → add `TwilioClient`/`MSG91Client` without touching callers. `_PROVIDER_MAP` in factory. |
| **L** — Liskov Substitution | `MSG91Client` and `TwilioClient` are drop-in `AbstractSMSClient` replacements. |
| **I** — Interface Segregation | Separate schema per operation: `CreateBookingRequest`, `BookingResponse`, `CancelBookingRequest` — no fat base class. |
| **D** — Dependency Inversion | Services take `AsyncSession` (abstraction). Routers depend on service interface. Celery uses `SyncSessionLocal`. |

### DRY Adherence

- `_log_transition` is the **single** audit trail writer — all 6 state transitions call it
- `_build_token_response` is called by both `verify_otp` and `refresh_tokens`
- `_otp_key` / `_attempt_key` in `otp.py` — Redis key strings built in one place only
- `NOTIFICATION_TEMPLATES` dict is the single source of truth for push notification copy
- `require_role` factory avoids repeating role checks across all admin routes
- `build_point_wkt` / `st_dwithin_filter` in `geo.py` — no WKT strings anywhere else

---

## API Routes (41 total)

### Auth
| Method | Path |
|--------|------|
| POST | `/api/v1/auth/send-otp` |
| POST | `/api/v1/auth/verify-otp` |
| POST | `/api/v1/auth/refresh` |

### Users
| Method | Path |
|--------|------|
| GET / PATCH | `/api/v1/users/me` |
| PATCH | `/api/v1/users/me/fcm-token` |
| GET / POST | `/api/v1/users/me/addresses` |
| DELETE | `/api/v1/users/me/addresses/{id}` |

### Providers
| Method | Path |
|--------|------|
| GET / PATCH | `/api/v1/providers/me` |
| PATCH | `/api/v1/providers/me/status` |
| GET | `/api/v1/providers/nearby` |

### Catalog
| Method | Path |
|--------|------|
| GET | `/api/v1/categories` |
| GET / POST | `/api/v1/services` |
| PATCH / DELETE | `/api/v1/services/{id}` |

### Bookings (state machine)
| Method | Path |
|--------|------|
| POST / GET | `/api/v1/bookings/` |
| PATCH | `/api/v1/bookings/{id}/accept` |
| PATCH | `/api/v1/bookings/{id}/en-route` |
| PATCH | `/api/v1/bookings/{id}/start` (OTP gate) |
| PATCH | `/api/v1/bookings/{id}/complete` |
| PATCH | `/api/v1/bookings/{id}/cancel` |

### Payments
| Method | Path |
|--------|------|
| POST | `/api/v1/payments/orders` |
| POST | `/api/v1/payments/verify` |
| POST | `/api/v1/payments/webhook` |

### Reviews
| Method | Path |
|--------|------|
| POST | `/api/v1/reviews/` |
| GET | `/api/v1/reviews/provider/{id}` |

### Tracking (WebSocket)
| Protocol | Path |
|----------|------|
| WS | `/api/v1/tracking/consumer/{booking_id}` |
| WS | `/api/v1/tracking/provider` |

### Admin
| Method | Path |
|--------|------|
| GET | `/api/v1/admin/users` |
| PATCH | `/api/v1/admin/users/{id}/deactivate` |
| PATCH | `/api/v1/admin/providers/{id}/kyc` |
| GET | `/api/v1/admin/bookings` |

---

## Bugs Fixed vs LLD

| LLD Bug | Fix Applied |
|---------|-------------|
| `hmac.new(key=..., msg=..., digestmod=...)` — wrong kwarg style | `hmac.new(key, msg, digestmod)` — positional args |
| `__import__("time").time()` in tracking service | Top-level `import time` |
| `SyncSessionLocal` not defined in `database.py` | Added sync engine + `SyncSessionLocal` auto-derived from async URL |
| `hmac.new` → actually correct stdlib form is `hmac.new` | Confirmed correct, kept |
| `require_role` was `async def` factory returning async dep — incorrect nesting | Made factory `def` (sync), inner `checker` stays `async def` |

---

## Verification Results

```
✅ uv sync — 94 packages installed, 0 conflicts
✅ All imports successful
✅ App created with 41 routes
✅ 9 tables in Base.metadata:
   addresses, booking_status_logs, bookings, categories,
   payments, provider_profiles, reviews, services, users
```

---

## Next Steps

```bash
# 1. Copy and fill in credentials
# Note: The actual `.env` file is intentionally added to `.gitignore` to prevent committing secrets.
# You MUST copy `.env.example` to `.env` to run the application locally.
cp .env.example .env

# 2. Start infrastructure
docker-compose up -d postgres redis

# 3. Run first Alembic migration
uv run alembic revision --autogenerate -m "initial"
uv run alembic upgrade head

# 4. Seed categories
uv run python scripts/seed_categories.py

# 5. Create super admin
uv run python scripts/create_admin.py +91XXXXXXXXXX

# 6. Start the API
uv run uvicorn app.main:app --reload

# 7. Run tests (requires running postgres + redis)
uv run pytest tests/ -v
```
