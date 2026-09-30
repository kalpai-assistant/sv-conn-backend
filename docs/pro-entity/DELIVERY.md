# Provider team model — delivery plan

Companion to `HLD.md` + `LLD.md`. Ticket delivery order + starter prompts for the tickets that most benefit from one.

Anyone picking up a ticket should read this file first, then the epic (SVC-318), then the specific ticket, then the HLD + LLD sections referenced.

---

## Ticket-by-ticket delivery order

**Legend:**
- 🟢 Ready now
- 🟡 Ready when SVC-339 lands
- 🔴 Ready only when Vipul signals an agency-onboard trigger
- ✅ Already Done

| # | Ticket | Phase | Status | Depends on | Owner | Effort |
|---|---|---|---|---|---|---|
| — | SVC-319 | A (old design) | ✅ Done | — | Tushar shipped | — |
| — | SVC-336 | Orthogonal | ✅ Done | — | Shipped | — |
| — | SVC-338 | Orthogonal | ✅ Done | — | Vipul + Claude shipped | — |
| **1** | **SVC-339** | **A (corrective)** | **🟢 SHIP NOW** | SVC-319 (Done) | **Tushar** | **~2 days** |
| **2** | **SVC-328** | B prereq | **🟢 START NOW (paperwork)** | — | **Vipul** | 30 min active + 1–3 day wait |
| — | *— waiting for agency-onboard trigger —* | | | | | |
| 3 | SVC-320 | B backend | 🔴 | SVC-339 + SVC-328 | Tushar | ~3–4 days |
| 4 | SVC-327 | B backend (KYC) | 🔴 | SVC-320 (can bundle into same PR) | Tushar | ~1 day |
| 5 | SVC-321 | B frontend | 🔴 | SVC-320 | Vipul | ~3 days |
| 6 | SVC-322 | B frontend | 🔴 | SVC-320 + SVC-328 | Vipul | ~4 days |
| 7 | SVC-329 | B admin | 🔴 | SVC-320 | Tushar + Vipul | ~2 days |
| 8 | SVC-323 | B nice-to-have | 🔴 | SVC-320 + SVC-321 | Tushar + Vipul | ~1 day |
| 9 | SVC-331 | B nice-to-have | 🔴 | SVC-320 + SVC-322 | Vipul | ~0.5 day |
| — | SVC-337 | Orthogonal | Filed | (not team-model) | — | — |
| 10 | SVC-324 | C backend | 🔴 (after B) | SVC-339 + SVC-320 | Tushar | ~0.5 day |
| 11 | SVC-325 | C frontend | 🔴 (after C1) | SVC-324 | Vipul | ~1.5 days |
| 12 | SVC-330 | C polish | 🔴 (after C1) | SVC-324 | Tushar | ~0.5 day |
| 13 | SVC-326 | D polish | 🔴 (much later) | SVC-320 + SVC-321 | Tushar + Vipul | ~0.5 day |
| 14 | SVC-332 | Docs | 🟡 anytime | — | Anyone | 30–60 min |

**Parallel windows once Phase B triggers:**
- **Wave 1** (foundation): SVC-320 + SVC-327 bundled → single PR. Blocks everything downstream.
- **Wave 2** (parallel after Wave 1): SVC-321, SVC-322, SVC-329, SVC-323, SVC-331 — split by owner. Three parallel work-streams.
- **Wave 3** (Phase C after Phase B is stable): SVC-324 → then SVC-325 + SVC-330 in parallel.
- **Wave 4** (polish): SVC-326, SVC-332. Whenever.

**Realistic Phase B ship time** with two people working parallel: ~2 weeks aggregate. Solo: 3–4 weeks.

---

## Starter prompts

Only three tickets get full starter prompts. Rationale:

- **SVC-339** — actionable now, and it's the corrective migration on live-payments beta. Wants the sub-agent-brief quality.
- **SVC-320** — biggest single ticket in the epic, central to Phase B. Everything downstream depends on it landing clean.
- **SVC-322** — biggest single frontend ticket, introduces a whole new `/pro/*` shell. Worth a prompt so the dev doesn't design the shell from scratch.

**All other tickets: the ticket description IS the prompt.** They're smaller, well-scoped, and reference the design docs. A dev reading the epic → HLD → LLD → ticket has everything they need.

---

### 🟢 SVC-339 — Phase A corrective (SHIP NOW)

- **Suggested agent:** `general-purpose`
- **Isolation:** `worktree`
- **Code:** yes
- **Blast radius:** medium — additive migration on live-payments beta (safe because additive)

```
You're picking up SVC-339 — the corrective Phase A migration for the Provider team model
(epic SVC-318). Repo: /Users/vips/svc_connect/backend (kalpai-assistant/sv-conn-backend).

READ FIRST — these are the source of truth. Do NOT implement from anything else:
- backend/docs/pro-entity/HLD.md — "V1 model" section at top
- backend/docs/pro-entity/LLD.md — "Schema", "Data migration", "Endpoints" sections
- SVC-339 ticket description in Jira for scope + acceptance
- SVC-319 (Done, PR #185) shipped the older single-entity design. Its parent_provider_id
  column + payout_target_id property are dormant legacy — do NOT touch or revert.

CORE WORK:

1. Alembic migration: create `professionals` table + `bookings.professional_id` +
   `booking_ratings.professional_id`. DDL is in LLD.md "Schema" section — copy exactly:
   - UNIQUE (user_id, service_provider_id) constraint
   - partial index on is_online WHERE is_online = TRUE
   - GIST index on last_location
   - ON DELETE CASCADE on service_provider_id FK

2. Data migration in the SAME alembic revision: for every provider_profiles row, INSERT a
   professionals row (self-Pro for solo case, same user_id). Use ON CONFLICT
   (user_id, service_provider_id) DO NOTHING for idempotency. Then backfill
   bookings.professional_id for every booking where provider_id IS NOT NULL AND
   professional_id IS NULL. Reference alembic/versions/f2b8d31c5a67_svc_286_category_hierarchy.py
   for the pattern of embedding data migration in a schema migration (op.execute with SQL).

3. New model app/models/professional.py — ORM class matching schema + relationships.

4. On ProviderProfile: add a `professionals` relationship (one-to-many).

5. Payout code: new writes should use booking.professional.service_provider_id.
   Keep the shipped ProviderProfile.payout_target_id property alive with a
   "# Deprecated — use booking.professional.service_provider_id going forward" comment.
   Do NOT remove the property yet. Cleanup ticket later.

6. Tests mirror the SVC-319 pattern in tests/test_provider_team_parent.py +
   tests/test_migrations.py. Cover: migration up+down, data migration idempotency, solo
   case has matching user_id, count(SPs) == count(Pros) post-migration, backfilled
   bookings have professional_id set.

7. Docs: backend/CLAUDE.md gets a paragraph under a "Two-entity model (SP + Pro)"
   subheading. What each table represents, that parent_provider_id is dormant legacy,
   how payout resolution flows. Not a novel.

DO NOT:
- Rename provider_profiles → service_providers (orthogonal, ~50 code sites; separate ticket)
- Remove the shipped parent_provider_id column or payout_target_id property
- Change bookings.status state machine (assigned → requested edge is Phase B)
- Change dispatch or FCM fan-out (Phase B)
- Modify API endpoints (Phase B)

RHYTHM:
1. Branch svc-339-phase-a-corrective-two-entity off main
2. cd backend && uv run alembic revision --autogenerate -m "add professionals table + booking professional_id (two-entity model)"
3. Verify autogen: partial index syntax + FK ondelete = CASCADE + UNIQUE constraint. Hand-edit if autogen missed anything.
4. Add app/models/professional.py + ProviderProfile.professionals relationship
5. Payout code refactor (grep, patch, verify)
6. Tests
7. uv run ruff check --fix . && uv run ruff format .
8. uv run pytest tests/ -v (846+ tests currently, must stay green)
9. uv run alembic check (must stay clean)
10. Commit with a "why" message: feat(providers): SVC-339 corrective Phase A — two-entity
    migration (professionals table, superseding SVC-319 single-entity design)
11. Push. gh pr create. Title references SVC-339 + Phase A corrective + supersedes SVC-319.
12. ADD THE db-migrate LABEL to the PR — not optional. Label check in ci.yml is a hard gate.
13. Wait for CI green. Vipul admin-merges.

KNOWN PITFALLS:
- Alembic autogen may miss the partial index — hand-edit if so
- Autogen may emit ondelete=None — verify FK constraint block
- Data migration must run inside op.execute(sa.text("INSERT ... ON CONFLICT DO NOTHING"))
- Solo case: user_id is the same on both SP + Pro rows; verify UNIQUE constraint allows this
- Payout code paths include Celery worker (SyncSessionLocal, not async engine) — verify
  the ORM relationship loads correctly in sync context

REPORT under 200 words with: PR URL, commit SHA, migration filename, files changed count,
any surprises. Confirm post-deploy verification: SELECT count(*) FROM provider_profiles
should equal SELECT count(*) FROM professionals.
```

---

### 🔴 SVC-320 — Phase B backend (start when trigger arrives + SVC-339 + SVC-328 done)

- **Suggested agent:** `general-purpose`
- **Isolation:** `worktree`
- **Code:** yes
- **Wait signal:** SVC-339 merged + agency-onboard trigger from Vipul + SVC-328 DLT template approved (template ID in SSM)

```
You're picking up SVC-320 — the backend surface for Phase B of the Provider team model
(epic SVC-318). Repo: /Users/vips/svc_connect/backend.

READ FIRST:
- backend/docs/pro-entity/HLD.md — "V1 model" + "Dispatch — two-level" + "Pro decline" sections
- backend/docs/pro-entity/LLD.md — "Endpoints", "Booking state machine", "Availability" sections
- SVC-320 ticket description in Jira for the endpoint list + guardrails
- SVC-327 (KYC semantics) — the invite-CREATE gate spec'd there SHOULD LAND WITH THIS TICKET
  (bundle into one PR)
- SVC-328 (DLT template) — MUST be Done before you can wire the invite SMS. Template ID lives
  in SSM at /eazyserv/beta/MSG91_INVITE_TEMPLATE_ID.

THREE BACKEND SURFACES TO BUILD IN ONE PR (or two if you split invite API from dispatch changes):

1. Team management endpoints:
   POST   /providers/me/team/invites            → creates invite + sends MSG91 SMS
   POST   /providers/me/team/invites/{id}/revoke
   GET    /providers/me/team                    → list Pros under my SP
   DELETE /providers/me/team/{pro_id}           → soft remove Pro
   POST   /providers/me/accept-invite           → invitee creates a professionals row

   NEW table `provider_team_invites` (design DDL yourself; LLD doesn't specify). Suggested:
   id UUID PK, invited_by UUID FK provider_profiles(user_id), invitee_phone VARCHAR(20),
   token VARCHAR(96) UNIQUE, status VARCHAR(16) DEFAULT 'PENDING',
   expires_at TIMESTAMPTZ NOT NULL, accepted_at, revoked_at,
   accepted_by_user_id UUID FK users, created_at TIMESTAMPTZ DEFAULT now().
   Partial unique index on (invited_by, invitee_phone) WHERE status='PENDING' to block
   duplicate live invites.

   Token: secrets.token_urlsafe(48). Single-use, 7-day expiry. Rate-limit create-invite
   at 5/hour per SP.

   Chain-depth check at accept-invite: reject if invitee already has a professionals row
   linked to another SP. Enforce in code, not DB CHECK.

   KYC gate at create-invite (from SVC-327): reject with 403 code
   INVITER_PAYOUT_KYC_INCOMPLETE if inviting SP's kyc_status != VERIFIED. Runs BEFORE
   the SMS is generated (no wasted SMS).

2. Two-level dispatch — the assign endpoint (NEW step in booking flow):
   POST /bookings/{id}/assign — atomic, first-write-wins
   Body: { professional_id }
   Verify: pro belongs to acting SP; booking.status='requested' (else 409 Conflict);
   pro is_online=true; pro has no other active job.
   Wrap in single transaction with SELECT ... FOR UPDATE on the booking row.
   On success: set booking.professional_id + booking.provider_id (from pro.service_provider_id)
   + status='assigned'. Emit FCM to consumer with Pro + SP details, emit FCM to assigned Pro.
   On race: 409 Conflict.

   Solo SP flow: /accept internally calls /assign with self-Pro (no picker step).
   Booking state machine: new edge assigned → requested when whole team declines
   (via Pro decline endpoint below).

3. Pro-side endpoints (NEW surface — first API prefix for Pros):
   GET  /pros/me                            → Pro profile
   GET  /pros/me/jobs                       → assigned + past
   POST /pros/me/online                     → { is_online }
   POST /pros/me/location                   → { lat, lng } heartbeat (every 30s while online)
   POST /pros/me/kyc                        → Aadhaar upload; identity KYC only (payout KYC
                                              lives on SP)
   POST /pros/me/jobs/{id}/decline          → SP notified; if whole team declined, booking
                                              transitions assigned → requested
   POST /pros/me/jobs/{id}/en-route
   POST /pros/me/jobs/{id}/start            → { doorstep_otp } → in_progress
   POST /pros/me/jobs/{id}/complete         → completed

RBAC:
- /pros/me/* auth requires user to have a professionals row
- /providers/me/team/* requires user to be an SP (existing provider role)
- /bookings/{id}/assign requires the caller to be the SP who was broadcast the booking
  (verify booking.dispatch record or similar; check existing dispatch code for the pattern)

DO NOT:
- Add any frontend (SVC-321, SVC-322 for that)
- Add consumer branding on bookings (SVC-324/325)
- Add admin surface (SVC-329)
- Add manual invite code entry (SVC-331)
- Rename provider_profiles table (orthogonal)
- Add cross-SP Pro linking (junction table when needed, not now)

RHYTHM:
1. Branch svc-320-phase-b-backend off main. Coordinate with Tushar on whether SVC-327 gets
   bundled (recommended: yes, one PR).
2. Design provider_team_invites DDL, generate alembic revision.
3. Build endpoints (start with team management, then Pro-side, then assign).
4. Wire MSG91 client at app/utils/sms_client.py for invite send (uses /api/v5/flow endpoint,
   NOT /api/v5/otp — see reference_msg91_otp_gotchas.md memory file).
5. Tests: race on /assign (two simultaneous POSTs, exactly one wins), chain-depth violation,
   KYC gate rejection, Pro decline chain, happy path end-to-end.
6. uv run ruff check --fix . && uv run ruff format .
7. uv run pytest tests/ -v
8. uv run alembic check
9. Commit + push + gh pr create + db-migrate label.
10. Vipul admin-merges after CI green.

KNOWN PITFALLS:
- MSG91 template must be in the SMS/Flow section on the dashboard, NOT the OTP section
  (see reference_msg91_otp_gotchas.md — the OTP endpoint is a silent-fail trap)
- Race on /assign — SELECT FOR UPDATE within a single transaction is the mechanism
- Solo SP: /accept internally calls /assign — verify no double-write
- Pro decline while assigned — must set bookings.professional_id = NULL AND transition
  status back to requested only if whole team has declined
- MSG91 blocked on SVC-328 approval — if SVC-328 not Done, this ticket is blocked

REPORT under 250 words with: PR URL, commit SHA, endpoints delivered, race-test result,
KYC-gate test result, whether SVC-327 was bundled or split, any surprises.
```

---

### 🔴 SVC-322 — Phase B frontend (Pro onboarding + Pro-side app UI)

- **Suggested agent:** `general-purpose` or a frontend-specialist
- **Isolation:** `worktree`
- **Code:** yes
- **Wait signal:** SVC-320 merged (backend endpoints live) + SVC-328 approved (invite SMS works)

```
You're picking up SVC-322 — the Pro-side frontend for Phase B of the Provider team model
(epic SVC-318). Repo: /Users/vips/svc_connect/frontend (kalpai-assistant/sv-conn-frontend).

READ FIRST:
- backend/docs/pro-entity/HLD.md — "V1 model" + "Dispatch — two-level" + "Consumer visibility"
- backend/docs/pro-entity/LLD.md — "Frontend structure", "Endpoints"
- SVC-322 ticket description in Jira for scope + guardrails
- SVC-320 (backend) must be merged and live on beta before you start — verify endpoints

TWO SURFACES TO BUILD:

1. INVITE-ACCEPT FLOW (invitee onboarding as a Pro):
   Deep link: https://app.eazyserv.com/invite/<token>
   Route: /invite/$token (TanStack Router)
   Flow:
   a. Tap link → app opens on /invite/$token
   b. If invitee has no account, standard OTP signup first (existing AuthFlow)
   c. Accept/decline screen showing SP brand + name + explanation ("accepting means you
      work as a Professional under this Service Provider")
   d. On accept: POST /providers/me/accept-invite { token } → backend creates a
      professionals row linked to inviting SP
   e. Land on Pro Home (see surface 2)

   SIGNUP BRANCH (important): if invite token is present at OTP-signup time (URL param or
   sessionStorage), skip the payout-details KYC section entirely. Pros do NOT collect bank/UPI
   at signup — payout routes to their SP. Aadhaar identity KYC is still required.

   Handle these four token error states with distinct copy:
   - expired
   - revoked
   - already-accepted
   - invalid
   Plus: if invitee already has a professionals row linked to another SP, backend returns 400
   with a specific error code — render a specific message, not a generic error.

2. PRO-SIDE APP UI (NEW /pro/* shell) — unlocks when user has a professionals row:
   Routes:
   - /pro                       → Home: currently assigned job or idle state
   - /pro/jobs                  → assigned + past jobs (from GET /pros/me/jobs)
   - /pro/profile               → Pro profile, online toggle
   - /pro/jobs/$bookingId       → active job detail: en-route + arrived + doorstep OTP entry + complete

   Components needed:
   - ProShell (routes wrapper, similar to /provider shell)
   - ProHome (idle / assigned state, subscribe to job updates)
   - OnlineToggle (POST /pros/me/online)
   - JobsList (GET /pros/me/jobs, paginated)
   - JobDetail (en-route / arrived / OTP entry / complete actions)
   - DeclineButton (POST /pros/me/jobs/{id}/decline, requires confirm modal)
   - LocationHeartbeat (background hook: POST /pros/me/location every 30s while is_online=true)

   SOLO SP SPECIAL CASE:
   Solo SP has both an SP row AND an auto-created Pro-for-self row. They should see the
   existing SP-side UI (Provider Home); the Pro-side actions (doorstep OTP entry, en-route,
   complete) fold into the SP home when it's their assigned job. NO visible role-switcher,
   NO separate /pro/* screen for solo SPs. Team-member Pros (invitee-flow signup) see ONLY
   /pro/* UI — no SP surface, no earnings dashboard, no team management.

   Role detection: after login, check if user has a professionals row + whether they have an
   associated SP row. The AuthContext / useApi should expose:
   - hasProRecord: boolean
   - hasSPRecord: boolean
   - hasTeamMembers: boolean (SP with children)
   Route to appropriate shell based on these flags.

DO NOT:
- Add SP-side UI (SVC-321 for My Team + pick-a-Pro modal)
- Add consumer branding (SVC-324/325 for that)
- Add manual invite code entry (SVC-331 — separate small ticket)
- Support multi-SP linkage (rejected in V1)
- Add advanced availability (schedules, calendars — V2)

RHYTHM:
1. Branch svc-322-pro-onboarding-and-app-ui off main
2. First: verify SVC-320 endpoints work on beta with curl (accept-invite happy path,
   /pros/me works with test JWT)
3. Deep link config: android/app/src/main/AndroidManifest.xml add intent-filter for
   https://app.eazyserv.com/invite/* with autoVerify=true. Host assetlinks.json at
   app.eazyserv.com root with SHA-256 fingerprint of release keystore.
4. Build Invite-accept flow, then Pro-side shell, then component-by-component
5. TESTS: RTL for the accept flow error states, JobsList rendering, OnlineToggle wiring
6. npx tsc --noEmit && npm run lint && npm test (all must pass)
7. Commit + push + gh pr create
8. DEVICE TEST on debug APK (Vipul convention — the pixel-level bugs only show on device):
   - Fresh install → tap invite link (deferred deep link) → app should open on /invite/$token
   - OTP signup → accept → Pro home
   - Existing user → accept → Pro home
   - Doorstep OTP entry works on real device
9. Vipul admin-merges after CI green + device test proof

KNOWN PITFALLS:
- Deferred deep link (fresh install, tap link, app not previously opened) — most fragile path.
  Test carefully.
- URL invite context must persist across OTP round-trip (10min Redis TTL) — sessionStorage
  or query param that survives
- Signup branch: payout KYC skip is a real regulatory choice — Vipul must OK the specific
  fields being skipped
- Solo SP + Pro-for-self: doorstep OTP entry must work from SP home (not just /pro/*).
  Do NOT create a role-switcher.
- Location heartbeat while backgrounded on Android — use a Capacitor background plugin or
  accept that heartbeat pauses when app is minimized (document either way)
- Existing Provider app users: they get an auto-created Pro-for-self from SVC-339 migration.
  Their first-open post-Phase-B ships should NOT show a re-onboarding screen. Verify.

REPORT under 250 words with: PR URL, commit SHA, files changed count, device test results
(fresh install + accept-invite + doorstep OTP), any UX judgment calls Vipul should review.
```

---

## All other tickets — the ticket description IS the prompt

Every remaining ticket (SVC-321, SVC-323, SVC-324, SVC-325, SVC-326, SVC-327, SVC-329, SVC-330, SVC-331, SVC-332) has a description with:

- Explicit dependencies + read-first refs
- Scope (bulleted)
- Non-goals (anti-scope-creep)
- Pitfalls (what to watch out for)
- Acceptance criteria

Any competent dev who reads the epic → HLD → LLD → the ticket has everything they need. No separate prompt required.

**Small gaps a dev may hit** (from the ticket-quality audit):

- **SVC-320**: `provider_team_invites` DDL not in the LLD — dev designs it (suggested shape is in the SVC-320 prompt above).
- **SVC-329**: `admin_audit_log` — dev checks if it exists first; designs if not.
- **SVC-322**: Pro-side wireframes don't exist yet (SVC-332 would add them). Dev designs shell from scratch OR waits for SVC-332.
- **SVC-324/325**: descriptions are shorter than SVC-320/322 due to JSON tool trouble during batch update. Scope is there; if a dev wants more detail, they read the LLD "Endpoints" + "Frontend structure" sections.

---

## Standard flow for any ticket

1. Read this file
2. Read SVC-318 (epic) for the model overview + ship state
3. Read `HLD.md` + `LLD.md` V1 sections
4. Read the specific ticket
5. If it's SVC-339, SVC-320, or SVC-322 → also read the prompt above
6. Branch, code, test, PR, add `db-migrate` label if migration, wait for CI, Vipul admin-merges

## When picking up a ticket that's not on this list

If a ticket references SVC-318 or `backend/docs/pro-entity/` but isn't listed above, it was filed after this doc was written. Follow the same flow — read the epic + design docs first, then the ticket. If anything is unclear, ask Vipul.
