# Service Provider + Professional — Low-Level Design

**Companion to HLD.md.** Corrected 2026-10-01 to match the two-entity two-level-dispatch model.

---

## Ship state as of 2026-10-01

The single-entity `parent_provider_id` design shipped as **SVC-319 / PR #185** on 2026-09-30, before the 2026-10-01 design correction. The corrective two-entity migration ships as **SVC-339** additively — SVC-319 artifacts (`provider_profiles.parent_provider_id` column, `ProviderProfile.payout_target_id` property) stay in place as harmless dormant legacy until a future cleanup ticket drops them. See HLD.md's "Important — ship state as of 2026-10-01" section for the rationale.

**What this means for the schema below:** every DDL example assumes the shipped SVC-319 state as the starting point. The `professionals` table + `bookings.professional_id` are the *net-new* additions from SVC-339. `parent_provider_id` on `provider_profiles` is not referenced in this LLD's data model going forward — it exists but is legacy.

---

## Schema

### service_providers

Same rows as today's `provider_profiles`. The SP is the payment target + brand holder.

**Rename decision (Phase A ticket)**: whether to `ALTER TABLE provider_profiles RENAME TO service_providers` + rename `ProviderProfile` model to `ServiceProvider`. Ripple cost is real (~50 code references) but the naming aligns with the new mental model. Recommend rename — cheap now, expensive later. Final call in SVC-319.

Existing columns unchanged. `business_name` + `business_image` come in via SVC-336 (orthogonal ticket, already in the epic).

### professionals — NEW

```sql
CREATE TABLE professionals (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_provider_id   UUID NOT NULL REFERENCES service_providers(user_id) ON DELETE CASCADE,
    user_id               UUID NOT NULL REFERENCES users(id),         -- auth account
    display_name          VARCHAR(120) NOT NULL,
    profile_photo_url     VARCHAR(512),

    -- Aadhaar identity KYC (payout KYC lives on the SP row)
    kyc_status            kyc_status_enum NOT NULL DEFAULT 'PENDING',
    kyc_docs              JSONB,
    kyc_ai_verdict        JSONB,                                       -- SVC-307 verifier output
    kyc_submitted_at      TIMESTAMPTZ,
    kyc_reviewed_at       TIMESTAMPTZ,
    kyc_reviewed_by       UUID REFERENCES users(id),
    kyc_rejection_reason  TEXT,

    -- Availability
    is_online             BOOLEAN NOT NULL DEFAULT FALSE,
    last_location         GEOGRAPHY(POINT, 4326),
    last_location_at      TIMESTAMPTZ,

    -- Team membership (SVC-320). Removal is a soft delete; bookings FK to the row.
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A user can be a Pro under exactly one SP at a time
    UNIQUE (user_id, service_provider_id)
);

CREATE INDEX ix_professionals_sp_id       ON professionals(service_provider_id);
CREATE INDEX ix_professionals_online      ON professionals(is_online) WHERE is_online = TRUE;
CREATE INDEX ix_professionals_location    ON professionals USING GIST (last_location);
```

- `service_provider_id NOT NULL` — every Pro belongs to an SP. No orphan Pros.
- `user_id` — auth account. For solo SPs, `user_id` == the SP's `user_id` (same person, two rows).
- KYC columns mirror the existing SP KYC surface but store the Aadhaar identity verification specifically for the Pro.
- Availability lives here (moved from `service_providers`) since it's the Pro-level signal — "is this specific person available to accept a job."
- `is_active` (SVC-320) — `false` once the SP removes the Pro from the team. The row stays because bookings and reviews reference it. The team list, and the one-SP-at-a-time rule at accept-invite, count active rows only. Re-inviting a removed Pro to the same SP flips the same row back (the UNIQUE pair allows only one).

### provider_team_invites — NEW (SVC-320)

```sql
CREATE TABLE provider_team_invites (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invited_by            UUID NOT NULL REFERENCES service_providers(user_id) ON DELETE CASCADE,
    invitee_phone         VARCHAR(20) NOT NULL,          -- canonical +91XXXXXXXXXX, same as users.phone
    token                 VARCHAR(96) NOT NULL,          -- 6-digit code, e.g. '004217'; single-use; NOT globally unique
    status                VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- PENDING | ACCEPTED | REVOKED | EXPIRED
    expires_at            TIMESTAMPTZ NOT NULL,          -- created_at + 7 days
    accepted_at           TIMESTAMPTZ,
    revoked_at            TIMESTAMPTZ,
    accepted_by_user_id   UUID REFERENCES users(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One live invite per (SP, phone); accepted / revoked / expired rows stay as history.
CREATE UNIQUE INDEX ux_provider_team_invites_pending
    ON provider_team_invites(invited_by, invitee_phone) WHERE status = 'PENDING';

-- Live codes are unique per phone: accept-invite looks up by (phone, code).
CREATE UNIQUE INDEX ux_provider_team_invites_pending_code
    ON provider_team_invites(invitee_phone, token) WHERE status = 'PENDING';
```

- `token` is a 6-digit numeric code (`f"{secrets.randbelow(1_000_000):06d}"`), not a URL token. STPL rejected every SMS template with a variable in the URL (SVC-328), so the SMS carries a static link plus the code. With only 10^6 codes, and history rows keeping theirs, a global `UNIQUE(token)` would turn into random insert failures. Live codes are unique *per phone* instead, and creation draws a fresh code on the rare collision.

- Expiry is lazy: a row stays `PENDING` past `expires_at` until an accept attempt or a re-invite of the same phone touches it.
- `status` is VARCHAR rather than a Postgres enum, so adding a state later needs no migration.

### bookings (modified)

```sql
ALTER TABLE bookings
    ADD COLUMN professional_id UUID NULL REFERENCES professionals(id);

CREATE INDEX ix_bookings_professional_id ON bookings(professional_id)
    WHERE professional_id IS NOT NULL;
```

- `professional_id` — nullable until the SP assigns a Pro (i.e. while `status = 'requested'`).
- **`provider_id` (existing) stays.** Still means "the SP who accepted." Set atomically alongside `professional_id` at assign-time. Kept for reporting + backwards compat.

### booking_ratings (modified)

```sql
ALTER TABLE booking_ratings
    ADD COLUMN professional_id UUID REFERENCES professionals(id);
```

Consumer rates the Pro. Existing `provider_id` on ratings stays for the SP aggregate (derived, not directly written).

## Data migration (Phase A)

One alembic revision, reversible, idempotent.

**Up:**

1. Create `professionals` + indexes.
2. Add `bookings.professional_id` + index.
3. Add `booking_ratings.professional_id`.
4. **Backfill Pros.** For every row in `service_providers` (= renamed `provider_profiles`), INSERT a `professionals` row:
   - `service_provider_id` = SP's `user_id`
   - `user_id` = same (solo case: SP is his own Pro)
   - `display_name` = users.name for the SP
   - `profile_photo_url`, `kyc_status`, `kyc_docs`, `kyc_ai_verdict`, `kyc_submitted_at`, `kyc_reviewed_at`, `kyc_reviewed_by`, `kyc_rejection_reason`, `is_online`, `last_location`, `last_location_at` — copy from the SP row
   - Skip if a `professionals` row already exists for this `(user_id, service_provider_id)` pair (idempotency).
5. **Backfill bookings.** For every booking with `provider_id IS NOT NULL AND professional_id IS NULL`, set `professional_id` to the single Pro under that SP.

**Down:**

1. Drop `booking_ratings.professional_id`
2. Drop `bookings.professional_id`
3. Drop `professionals`

Idempotency (step 4): the `UNIQUE (user_id, service_provider_id)` constraint means re-running INSERT is safe with `ON CONFLICT DO NOTHING`.

## Endpoints

### Dispatch flow

```
POST /bookings                          → Consumer creates booking (existing, unchanged)
[Dispatch fan-out]                      → Celery task; FCM to eligible SPs (existing shape, targets SPs)

POST /bookings/{id}/accept              → SP taps "accept"
    - Solo SP: server calls /assign internally with the SP's Pro-for-self
    - Team SP: no DB write; response is 200 with team-Pros list for the pick modal

POST /bookings/{id}/assign              → SP picks a Pro (atomic, first-write-wins)
    Body: { professional_id }
    Guardrails:
    - Auth: acting user must be the SP (booking's dispatch target)
    - Verify professional_id belongs to acting SP
    - Verify booking.status = 'requested' (else 409 Conflict — already assigned)
    - Verify Pro is is_online = true and has no other active job
    Actions (single transaction, SELECT ... FOR UPDATE on booking):
    - booking.professional_id = pro.id
    - booking.provider_id = pro.service_provider_id
    - booking.status = 'assigned'
    - Emit FCM to consumer with Pro + SP details
    - Emit FCM to assigned Pro
```

### Pro-side app (NEW)

```
GET  /pros/me                           → my Pro profile
GET  /pros/me/jobs                      → active + past jobs assigned to me
POST /pros/me/online                    → { is_online: bool }
POST /pros/me/location                  → { lat, lng } heartbeat (every 30s while online)
POST /pros/me/kyc                       → same shape as /providers/me/kyc (Aadhaar upload)
POST /pros/me/jobs/{id}/decline         → decline assigned job → SP notified for re-pick
POST /pros/me/jobs/{id}/en-route        → transition to en_route
POST /pros/me/jobs/{id}/start           → { doorstep_otp } → in_progress
POST /pros/me/jobs/{id}/complete        → completed
```

### SP team management (Phase B) — shipped in SVC-320 PR 1

```
POST   /providers/me/team/invites       → { invitee_phone } SMS invite; returns token (the code) + invite_url (static)
POST   /providers/me/team/invites/{id}/revoke
GET    /providers/me/team               → active Pros under my SP, Pro-for-self (is_self) first
DELETE /providers/me/team/{pro_id}      → remove Pro (soft delete: set is_active=false)
POST   /providers/me/accept-invite      → { token: "<6-digit code>" } invitee accepts → new (or reactivated) Pro row
```

- **Create** (role `provider`): rate-limited to 5/hour per SP. The KYC gate runs first (403 `INVITER_PAYOUT_KYC_INCOMPLETE` unless the SP's `kyc_status = VERIFIED`), before any code or SMS. Self-invite is a 400; a phone already active on the team, or with a live invite, is a 409.
- **SMS**: MSG91 `/api/v5/flow` with template "{var1} invited you to join their team on EazyServ. Open https://app.eazyserv.com/invite and enter {var2}. Expires in 7 days." `var1` = SP business name (truncated to DLT's 30-char cap), `var2` = the code. The URL is fully static (`INVITE_URL` in `app/models/provider_team_invite.py`). It opens the app rather than the browser only once Android App Links are set up for `app.eazyserv.com` (**SVC-343**). While `MSG91_INVITE_TEMPLATE_ID` is empty (until the SVC-328 DLT approval), the invite is only logged as `[MOCK INVITE] SP=… phone=… code=… url=…` and MSG91 is never called.
- **Accept**: open to role `consumer` *or* `provider` (the app signs everyone up as a consumer). Rate-limited to 10/hour per user (so per phone), and every call counts, wrong and malformed codes included: that is what makes 10^6 codes safe. The invite is looked up by code **and** the caller's own phone, so a code only works for the person it was texted to. 404 if it isn't one of the caller's invites, including someone else's valid code: same body, so nothing leaks. 410 if it was the caller's but is used, revoked or expired. 422 for anything but 6 digits. 400 `ALREADY_PRO_ELSEWHERE` if the caller has an *active* Pro row under another SP, and an SP's own Pro-for-self counts, so an existing SP can't join a team. 409 if already active on this team.
- **Remove**: an SP can't remove its own Pro-for-self (400 `CANNOT_REMOVE_SELF`). Going off-tools is SVC-326, which brings the dispatch filter it needs.
- **Pro-for-self at signup**: `ProviderService.get_or_create_profile` creates it in the same transaction as the SP row.

## Booking state machine (updated)

> **Naming:** `assigned` in this doc is `BookingStatus.ACCEPTED` in code. There is no separate `ASSIGNED` value; the "SP picked a Pro" step writes `ACCEPTED`.

```
requested (broadcast open)
    │
    │ SP calls /assign with a Pro
    ▼
assigned  ◀───────────────────────────┐
    │                                 │
    │ Pro accepts + starts moving     │ Pro declines
    ▼                                 │ (SP re-picks; whole team declined → back to requested)
en_route                              │
    │                                 │
    │                                 │
    ▼                                 │
in_progress (doorstep OTP entered)    │
    │                                 │
    ▼                                 │
completed                             │
                                      │
(cancelled reachable from requested / assigned / en_route)
```

**New edge: `assigned → requested`.** Triggered by (a) all Pros in team declining, or (b) explicit SP escalation. Booking becomes broadcast-eligible again.

## Frontend structure

**SP-side (existing `/provider/*` shell, mostly unchanged):**
- Home — current jobs + accept broadcasts + earnings
- **NEW: pick-a-Pro modal** overlay on `accept` (for team SPs only). Solo SPs skip this modal entirely — accept fires assign in one step.
- **NEW: "My Team" screen** (Phase B) — list Pros, invite, remove
- Earnings — includes per-Pro breakdown for team SPs (Phase B)

**Pro-side (NEW `/pro/*` shell, unlocks when user has a `professionals` row):**
- Home — currently assigned job or "idle, waiting for work"
- Online toggle
- Jobs list (assigned to me)
- Doorstep OTP entry when at customer address
- Location heartbeat (background)

**Solo SP special case.** Solo SP has both an SP row and a Pro-for-self row. They see the SP-side UI; the Pro-side actions are folded in transparently (e.g. the doorstep OTP entry screen appears from the SP home when it's their assigned job). No visible role-switcher.

**Team-member Pro.** Sees only the `/pro/*` UI. No SP surface, no invite screens, no earnings dashboard (their earnings settle off-platform via their SP).

## Availability model

V1: `professionals.is_online` boolean. Set by Pro toggling online in-app. Dispatch broadcast filters SPs by "has ≥1 online Pro" (perf: filter in the dispatch SQL, don't post-load). SP's pick-a-Pro modal grays out offline Pros.

V2 (deferred): schedule-based availability, advance-booking calendars, blackout windows. Not V1.

**Location heartbeat.** While `is_online = true`, Pro app POSTs `/pros/me/location` every 30 seconds. If silent > 5 minutes, dispatch treats them as effectively offline (stale-connection guard).

## KYC

- **Pro row's `kyc_status`**: driven by Aadhaar identity verification via the existing AI KYC verifier (SVC-307). Same enum: `PENDING`, `VERIFIED`, `REJECTED`.
- **SP row's `kyc_status`**: driven by business identity + bank/UPI verification (existing SP onboarding).
- **Solo case**: same user completes both flows; two separate KYC records that happen to share a `user_id`.
- **Team member Pro**: only Aadhaar. Payout KYC is the inviting SP's responsibility.
- **Enforcement**: `POST /providers/me/team/invites` rejects with 403 if the inviting SP's `kyc_status != VERIFIED` — their team's payouts would fail otherwise.

## Payout resolution

```python
# Every booking payout routes through:
payout_target_sp = booking.professional.service_provider  # always the SP
```

Simple. For solo bookings, `payout_target_sp.user_id == booking.provider_id == booking.professional.user_id` — same person. For team bookings, the target is the team-owner SP regardless of which Pro accepted.

## Testing surface

**Unit:**
- `Professional.service_provider` relationship loads correctly
- `/assign` atomicity — two simultaneous POSTs to the same booking, only one gets 200, other gets 409
- Pro decline → SP re-pick chain → whole-team decline → booking transitions back to `requested`
- Solo SP end-to-end: single user_id, SP + Pro rows, correct payout target
- Team SP end-to-end: multiple Pros, correct assignment + payout resolution

**Integration:**
- Full booking flow: broadcast → SP accept → SP assign Pro → en_route → doorstep OTP → completed
- Race test: two SPs simultaneously accept + assign; verify exactly one wins
- Migration idempotency: run twice against a fixture DB, no duplicate Pro rows

**Migration validation:**
- Every `service_providers` row has a corresponding `professionals` row post-up
- Every booking with `provider_id` has `professional_id` post-up
- Downgrade + re-upgrade produces the same state (idempotency)

## What breaks + how we hide it

- **Existing Provider app users** — see the same UI. Solo case is transparent: they're still an SP + they're also auto-registered as their own Pro. No re-onboarding.
- **Existing bookings** — backfilled `professional_id` at migration; UI keeps rendering the same info (Pro is the SP, one and the same).
- **Payout code** — resolves through `professional.service_provider_id` which for existing bookings equals the old `booking.provider_id`. No behavior change.
- **Existing dispatch code** — broadcasts to SPs as before. The only wire-level change is `bookings.professional_id` is now written at accept-time.

Phase A alone is genuinely invisible to users. Phase B unlocks team support.

## Cost / infra impact

- New table + indexes: negligible on RDS.
- Location heartbeats: 1 write per online Pro per 30s. At 100 online Pros = 200 writes/min. Very low.
- FCM broadcast size unchanged (still one push per SP, not per Pro).
- No new external services.

---

## Historical drafts (superseded, retained for context)

<details>
<summary><strong>Original two-entity M:N draft (2026-09-23)</strong></summary>

Same shape but with `provider_professionals` junction table for M:N linking. Would let a Pro work under multiple SPs. Rejected — Vipul confirmed 1:N is sufficient for V1; cross-SP gig workers are a future problem, add a junction table then.

</details>

<details>
<summary><strong>Single-entity `parent_provider_id` design (2026-09-25 to 2026-10-01)</strong></summary>

Collapsed both entities into one `provider_profiles` table with a nullable `parent_provider_id` self-FK. Solo/team-owner/team-member were three states of one entity.

Rejected: didn't match Vipul's actual mental model. Also unable to model the two-level dispatch flow (broadcast to SPs → SPs pick a Pro from team) without materializing a distinct Pro entity anyway. The "collapse" turned out to be structurally wrong for the flow.

</details>
