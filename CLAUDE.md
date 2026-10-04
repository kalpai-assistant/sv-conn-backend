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

Service steps/exclusions text (SVC-340) lives in `app/seed_data/service_details.py`; the migration and both service seed scripts apply it. For new wording, edit that file and add a small migration that re-runs `UPDATE_SERVICE_DETAILS`.

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
    ▲          │     ↘                         ↗
    └─reopen───┘      cancelled (from most states)
```

`accepted → requested` (SVC-320) is driven only by `BookingService.reopen` (a Pro decline with nobody left on the team, or an SP release), never through `transition_status`. The offer window runs from `Booking.offer_window_start` = `COALESCE(reopened_at, created_at)`, so a reopened booking gets a fresh two minutes.

**Paid bookings nobody takes (SVC-344).** The 30 s `expire_stale_bookings` sweep expires *unpaid* bookings as before. A PAID one gets two steps instead, each in its own transaction via `BookingService.advance_paid_unclaimed`: first a second offer (`last_chance_at` is set and it's re-broadcast, giving `BOOKING_LAST_CHANCE_SECONDS`=90 more), then a system cancel with a full refund through `_apply_cancellation_policy`. That logs `changed_by` NULL (= system) and sends a consumer-only `no_taker_refunded` push, worded differently if the booking had been reopened. A refund failure rolls back and the next sweep retries. Every expiry reader uses `Booking.offer_expires_at`. A reopen sets a new doorstep OTP and clears `last_chance_at`. A provider's `PATCH /cancel` while ACCEPTED reopens the booking instead of cancelling it (no fee); while EN_ROUTE it's a 409 `PROVIDER_EN_ROUTE`. Consumer cancel is unchanged.

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
- **Service steps / exclusions (SVC-340)**: nullable JSONB lists on `services`, returned only by `GET /services/{id}` (`ServiceDetailResponse`), never the list. NULL = hide the section. Edited by SQL only (no admin UI); seed rows are keyed by (category slug, service name) since `services` has no slug.
- **Geo queries**: PostGIS-backed; `app/utils/geo.py` builds WKT geometries; nearby provider search uses `ST_Distance`
- **PII encryption**: AES-256 via `app/utils/encryption.py` for sensitive fields at rest
- **WebSocket auth**: JWT passed as `?token=<jwt>` query param, validated per connection in `app/routers/tracking.py`
- **Celery + async DB**: Workers use `SyncSessionLocal` (derived from async URL) since Celery is sync
- **CORS**: `CORS_ALLOWED_ORIGINS` env var, comma-separated. Dev default in `config.py` covers localhost + Capacitor + prod web. Wildcard deliberately not accepted (spec violation with `allow_credentials=True`).

### Provider team model (SVC-318)

Design: `docs/pro-entity/HLD.md` + `LLD.md`; ticket order in `DELIVERY.md`.

#### Two-entity model (SP + Pro) — SVC-339

A **Service Provider (SP)** is the business: it receives dispatch and is paid. It is still the `provider_profiles` table and `ProviderProfile` model; the rename to `service_providers` is deliberately a separate ticket. A **Professional (Pro)** is the person at the door: `professionals` / `Professional`, belonging to exactly one SP via `service_provider_id` → `provider_profiles.user_id` (`ON DELETE CASCADE`, `UNIQUE (user_id, service_provider_id)`). Every SP has a **Pro-for-self** sharing its `user_id` — the solo case is one person, two rows; the SVC-339 migration created one for every provider and pointed every existing booking's `bookings.professional_id` at it. `reviews.professional_id` (the docs call that table `booking_ratings`) is the Pro being rated. **Payout resolution**: the payee is `booking.professional.service_provider_id`, never the Pro; for a solo booking that equals `booking.provider_id`, which today's payout code still reads. Phase A is schema only — nothing creates a Pro for a provider who signs up after the migration, and nothing writes `bookings.professional_id` for new bookings, until Phase B's onboarding and `/assign` work. Until then a NULL `professional_id` is normal.

**Phase B PR 1 (SVC-320 + SVC-327):** `ProviderService.get_or_create_profile` now creates the Pro-for-self along with the SP row, and the SVC-320 migration backfills providers (and their accepted bookings) created between the two deploys. Team management lives in `app/services/team_service.py` (`/providers/me/team*` + `/providers/me/accept-invite`). An invite needs a VERIFIED SP: otherwise 403 `INVITER_PAYOUT_KYC_INCOMPLETE`, checked before any code or SMS. An invite is a 6-digit numeric code (Vipul, SVC-320 comment 14273), single-use, valid 7 days. Creating is rate-limited to 5/hour per SP; accepting to 10/hour per user, with wrong codes counting. Accept looks up by code **and** the caller's own phone, so another user's code is a plain 404. Live codes are unique per phone, not globally, so history rows can reuse a code. Accept is open to consumers too, because the app signs everyone up as one. Removal is soft (`professionals.is_active = false`), and an SP can't remove its own Pro-for-self; owner-off-tools is SVC-326. One active Pro row per user. **Mock invite SMS:** with `MSG91_INVITE_TEMPLATE_ID` empty, `sms_client.send_team_invite` only logs `[MOCK INVITE] SP=… phone=… code=… url=…`. Set it (SSM `/eazyserv/beta/MSG91_INVITE_TEMPLATE_ID`, after SVC-328's DLT approval) and real sends go out via MSG91 `/api/v5/flow`. That template is unrelated to `MSG91_TEMPLATE_ID`, which is the OTP template. The SMS link is the fully static `INVITE_URL` (`https://app.eazyserv.com/invite`), because STPL rejects any variable in a URL (SVC-328). `var1` is the business name, truncated to DLT's 30-char cap, and `var2` is the code. The link opens the app only once SVC-343 (Android App Links) lands. In the docs, `assigned` means `BookingStatus.ACCEPTED`.

**Phase B PR 2 (two-level dispatch):** `PATCH /accept` claims with the SP's Pro-for-self as `professional_id`, for solo and team SPs alike, and repairs a missing Pro-for-self via `ProviderService.ensure_self_professional`. Only a client sending `X-Team-Pick: 1` gets the 202 pick list instead, because older builds treat any 2xx as a win. `POST /bookings/{id}/assign` is the claim with a chosen Pro, or a re-pick on a booking the SP already holds; it has the `PRO_BUSY` check, which accept does not. `POST /pros/me/jobs/{id}/decline` authorizes on the Pro row, not a role. `POST /bookings/{id}/release` gives a job back. Re-broadcasts use `broadcast_recipients_stmt` (eligible minus SPs who answered). New pushes: `assigned`, `pro_declined`, `reopened`. 

**Phase B PR 3 (Pro surface + Pro KYC):** `app/routers/pros.py` + `app/services/pro_service.py`. Every `/pros/me/*` endpoint authorizes on the caller's ACTIVE `professionals` row (`get_current_pro`), never a role. `GET /bookings/{id}` and `transition_status` admit the booking's assigned active Pro (`BookingService.is_assigned_pro`), and `viewer_for` renders them the provider's view. The en-route/start/complete side effects live in one helper, `notify_transition` in `routers/bookings.py`. Only the provider or the assigned Pro can move a job; the consumer no longer can. A Pro-for-self follows its SP: online mirrors the SP toggle, and its KYC is the SP's. KYC is row-generic (`store_kyc_submission`). Admin reviews Pro KYC at `/admin/pros/{professional_id}/kyc*`, and the queue's `?kind=` is opt-in. `/assign` returns 409 `PRO_OFFLINE` for an offline team Pro.

**Assignment audit trail (SVC-333):** every change of a booking's Pro writes one append-only `booking_assignment_events` row (`claimed`, `repicked`, `declined`, `released`, `provider_cancelled`; from/to Pro, actor, NULL = system) through `BookingService.record_assignment`, the only writer, in the same transaction as the change. A same-Pro re-assign, a failed assign, or a system/admin/consumer cancel writes nothing. Deliberately not `booking_status_logs`, which feeds the consumer's `status_history`. Admin reads it merged with the status rows at `GET /admin/bookings/{id}/timeline`; same-transaction ties sort a Pro leaving before the status row and a Pro arriving after it.

#### Legacy: single-entity design — SVC-319 (superseded)

Dormant; a cleanup ticket drops it. `provider_profiles.parent_provider_id` is a nullable self-reference to the team owner's `provider_profiles.user_id` (`ON DELETE SET NULL`). `NULL` means a solo provider *or* a team owner; the two look the same until a member points at the owner. Set means a team member. Everyone keeps the single `"provider"` role, and dispatch is unchanged. The payout rule lives in one place, `ProviderProfile.payout_target_id`: `parent_provider_id or user_id`, so a member's earnings belong to their owner. `booking.provider_id` keeps meaning "who accepted the job". No code reads `payout_target_id`, and it is deprecated: route payee lookups through `booking.professional.service_provider_id` instead. Every row's `parent_provider_id` is NULL. Chain depth is 1, enforced in service code at invite-accept time (Phase B), not by a DB constraint.

### Infrastructure (docker-compose)

5 services: `api`, `celery-worker`, `celery-beat`, `postgres` (PostGIS image), `redis`. Postgres requires the PostGIS extension — do not use a plain postgres image.

### Beta DB (RDS, SVC-304 + SVC-305)

Beta backend runs on App Runner (`eazyserv-backend` + `eazyserv-worker`) in `ap-south-1`. Postgres moved from Neon to AWS RDS on 2026-09-18 after Neon's free-tier compute quota was exhausted (SVC-304). SVC-305 (2026-09-25) then tightened the networking off `0.0.0.0/0`.

- **Instance:** `eazyserv-beta-postgres` (PostgreSQL 16.15, db.t3.micro, gp3 20 GB, single-AZ)
- **Endpoint:** `eazyserv-beta-postgres.cruawyqc2v1u.ap-south-1.rds.amazonaws.com:5432`
- **DB / user:** `eazyserv` / `eazyserv_admin`
- **Master password:** SSM SecureString `/eazyserv/beta/RDS_MASTER_PASSWORD`
- **Connection URLs:** SSM `/eazyserv/beta/DATABASE_URL` (asyncpg, `?ssl=require`) and `/eazyserv/beta/SYNC_DATABASE_URL` (psycopg2, `?sslmode=require`)
- **Networking (SVC-305):**
  - `PubliclyAccessible=false` — endpoint DNS resolves to a private VPC IP only; unreachable from the public internet.
  - Active SG: `eazyserv-beta-rds-private-sg` (`sg-0c137ea3e4a77fec4`). Only inbound rule: `5432/tcp` from `eazyserv-beta-apprunner-connector-sg` (`sg-0e56366ab2ca979ea`).
  - App Runner path: both `eazyserv-backend` and `eazyserv-worker` run with `EgressType=VPC` on the App Runner VPC Connector `eazyserv-beta-connector` (`arn:aws:apprunner:ap-south-1:018272776203:vpcconnector/eazyserv-beta-connector/1/5037c8cd1d6343b89dc34e65834966e8`). The connector's ENIs live in the private subnets `subnet-0adc6a3d6e5db46f3` (ap-south-1a) + `subnet-06785632e8fac8e19` (ap-south-1b), which route `0.0.0.0/0` via NAT Gateway `nat-08bea3c684076467d` (EIP `13.235.105.131`) for outbound calls to Razorpay / MSG91 / FCM / SSM / etc.
  - The RDS instance itself stayed in the `default` subnet group (AWS refuses cross-subnet-group swaps within the same VPC). Security posture is equivalent because RDS has no public IP and its SG only lets the connector SG in — the "public" subnet designation is only about the subnet's route table, which the RDS ENI never uses inbound.
  - Retired: `eazyserv-beta-rds-sg` (`sg-0eb76db639631b190`) is detached from the instance and its `0.0.0.0/0:5432` rule was revoked. SG is kept empty as a paper trail; safe to delete after any followup review.
- **Guardrails:** `DeletionProtection=True`, `StorageEncrypted=True`, `rds.force_ssl=1` (parameter group `eazyserv-beta-postgres16`), 7-day PITR, auto-minor-version-upgrade on.

#### psql from a workstation (via SSM bastion tunnel)

There is no direct path from a workstation to RDS anymore. Spin an ephemeral t4g.nano bastion in the private subnet, port-forward through SSM Session Manager, then terminate. Cost: ~₹0.60 / hour. Do NOT leave the bastion running.

```bash
export AWS_PROFILE=eazyserv AWS_DEFAULT_REGION=ap-south-1

# 1. One-time (first use): create an IAM role + instance profile that grants
#    the bastion the SSM Session Manager permissions. If it already exists,
#    skip — reuse the same role/profile every time.
aws iam create-role --role-name eazyserv-beta-bastion-role --assume-role-policy-document '{
  "Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' 2>/dev/null || true
aws iam attach-role-policy --role-name eazyserv-beta-bastion-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore 2>/dev/null || true
aws iam create-instance-profile --instance-profile-name eazyserv-beta-bastion-profile 2>/dev/null || true
aws iam add-role-to-instance-profile --instance-profile-name eazyserv-beta-bastion-profile \
  --role-name eazyserv-beta-bastion-role 2>/dev/null || true

# 2. Launch a t4g.nano in a private subnet using the latest Amazon Linux 2023
#    ARM AMI. Connector SG has no inbound but egress-all, which is fine —
#    SSM connects outbound to the SSM endpoints via NAT.
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameter.Value' --output text)
INSTANCE=$(aws ec2 run-instances --image-id "$AMI" --instance-type t4g.nano \
  --subnet-id subnet-0adc6a3d6e5db46f3 \
  --security-group-ids sg-0e56366ab2ca979ea \
  --iam-instance-profile Name=eazyserv-beta-bastion-profile \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=eazyserv-beta-bastion},{Key=env,Value=beta},{Key=purpose,Value=svc-305-psql-tunnel}]' \
  --query 'Instances[0].InstanceId' --output text)
echo "bastion instance: $INSTANCE"
aws ec2 wait instance-running --instance-ids "$INSTANCE"
# SSM registration takes an extra ~60s after "running"
for i in $(seq 1 20); do
  s=$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE" \
        --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)
  echo "ssm ping: $s"
  [ "$s" = "Online" ] && break
  sleep 10
done

# 3. Port-forward RDS 5432 → localhost:15432 via Session Manager.
#    Requires the Session Manager plugin locally:
#      brew install --cask session-manager-plugin
aws ssm start-session --target "$INSTANCE" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters host=eazyserv-beta-postgres.cruawyqc2v1u.ap-south-1.rds.amazonaws.com,portNumber=5432,localPortNumber=15432

# 4. In a second terminal: psql to localhost:15432 as if it were RDS.
export PGPASSWORD=$(aws ssm get-parameter --name /eazyserv/beta/RDS_MASTER_PASSWORD \
  --with-decryption --query 'Parameter.Value' --output text)
psql "host=localhost port=15432 user=eazyserv_admin dbname=eazyserv sslmode=require"

# 5. When done: Ctrl-C the port-forward, then terminate the bastion.
aws ec2 terminate-instances --instance-ids "$INSTANCE"
```

### Migrations on beta

**Commit the migration, merge, and the API container runs it on boot.**
`docker-api-entrypoint.sh` runs `alembic upgrade head` unconditionally
every time the backend App Runner container starts. Alembic is idempotent
— when the schema is already at HEAD, it's a ~1-second no-op — so this is
safe on every deploy, every scale-up, every restart.

Only the API service migrates. The Celery worker uses a different CMD
(`docker-worker-entrypoint.sh`) that never touches alembic, deliberately
— see that file's header for the "don't add it for symmetry" note.

Rollback story: a failed `alembic upgrade head` exits the entrypoint
non-zero, App Runner marks the deploy failed and auto-rolls back to the
previous image. Beta keeps serving the old code against its own schema —
same "failed migration stops the deploy" invariant we had before SVC-305.

The `db-migrate` label is still required on any PR that adds a migration
file (enforced by `migration-label-check` in `ci.yml`). It has no runtime
effect anymore — the container always migrates on boot — but it forces
the human to confirm the schema change is intentional. Forgetting to
apply a schema change is what bit PR #175 (`kyc_ai_verdict` missing from
beta for ~20 hours); the label + on-boot alembic together make both
"missing migration" and "unapplied migration" fail loudly.

**Manual alembic (rollbacks, out-of-band schema surgery):** you cannot
reach RDS from a workstation directly anymore (SVC-305 tightened it to
the App Runner connector SG only). Use the SSM-bastion recipe from the
"Beta DB (RDS, SVC-304 + SVC-305)" section above to tunnel 5432 through,
then use `ALEMBIC_DATABASE_URL` (psycopg2, `?sslmode=require`) to point
alembic at the tunnel:

```bash
export ALEMBIC_DATABASE_URL=$(aws ssm get-parameter --name /eazyserv/beta/SYNC_DATABASE_URL \
  --with-decryption --query 'Parameter.Value' --output text \
  | sed 's|@eazyserv-beta-postgres[^/]*|@localhost:15432|')
uv run alembic downgrade -1      # or `upgrade <revision>` for a specific target
```

`alembic check` in ci.yml catches a *missing* migration at PR time; the
on-boot alembic + App Runner auto-rollback catches an *unapplied* one at
deploy time.

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
