# Multi-device push tokens — scope + proposal

Spike doc (SVC-353). Sizes the "one `users.fcm_token` column, one device" gap
against real beta data, proposes a schema if we fix, and names follow-up build
tickets. No code shipped here.

> Scope note: backend only. The client registers whichever token Firebase
> hands it (native via `@capacitor/push-notifications`, web via `firebase/
> messaging`) and `PATCH`es it to `/users/me/fcm-token`. The storage shape
> on the server is what forces single-device behaviour, so that is what this
> doc interrogates.

## 1. How many beta users actually have >1 device today?

Queried beta Postgres on 2026-10-14 via the SSM-bastion recipe in
[CLAUDE.md](../CLAUDE.md).

| role | total | with `fcm_token` | last login ≤ 30d | logged-in-and-live |
| --- | ---: | ---: | ---: | ---: |
| consumer | 9 | 5 | 8 | 5 |
| provider | 2 | 1 | 2 | 1 |
| admin | 1 | 0 | 1 | 0 |
| **total** | **12** | **6** | **11** | **6** |

Historical signal — **none**. The database has one place a device token
could live and one row per user to hold it:

- `users.fcm_token` is a single `String(255)` column
  ([app/models/user.py:27](../app/models/user.py#L27)), set from
  `PATCH /users/me/fcm-token`
  ([app/services/user_service.py:94-107](../app/services/user_service.py#L94-L107)).
  The write overwrites in place; the previous value is lost.
- `UPDATE users` goes through SQLAlchemy's `onupdate=func.now()` on
  `updated_at` ([app/models/user.py:32-34](../app/models/user.py#L32-L34)),
  so **every** write to the user row — profile edit, FCM token swap, SVC-240
  `last_login_at` bump on verify-otp — moves `updated_at`. The column
  therefore carries no evidence that the token specifically changed.
- Only one other table in the schema has a column that looks like it might
  track a token: `provider_team_invites` (SVC-320's invite codes). Not FCM.
- Structlog records are not in Postgres; they go to CloudWatch via the
  App Runner log driver (SC-46). CloudWatch Insights has a 30-day retention
  by default and the token itself is redacted at the processor
  ([app/logging_config.py](../app/logging_config.py)), so even a trawl could
  not reconstruct per-user token history.

**No historical signal available.** On first principles: pros rarely
re-install (one business device, treated like equipment); consumers
re-install on a new phone every ~18 months. Expect <5% overlap at any one
time across the user base, and *zero* observable overlap on 12 users.

**Verdict for §1: defer-with-trigger.** 12 users is too few to say
anything with confidence, and there is no evidence that any of them owns
two signed-in devices. The fix is cheap to design (§2) and medium-cheap to
build (§3), but the carrying cost of *not* building it is one dropped
notification per user per device-swap event, which the existing SVC-301
dead-token-clearing path already partially masks: a dead token gets nulled
on the next send attempt and the user's next registration from the new
device re-populates the column. The pathological case ("I'm signed in on
phone + tablet simultaneously and only one gets the push") is not visible
in the data. Trigger a build when a user complaint arrives, or on the
first provider onboarding batch >100 (whichever comes first) — see §5.

## 2. Proposed schema if we fix

```sql
CREATE TABLE device_tokens (
    id            UUID PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token         TEXT NOT NULL UNIQUE,
    platform      TEXT NOT NULL CHECK (platform IN ('android', 'ios', 'web')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX idx_device_tokens_user_live
    ON device_tokens(user_id) WHERE revoked_at IS NULL;
```

The partial index is the one every send path reads through; a non-partial
index on `user_id` would pull revoked rows and force the caller to filter
in Python. Soft-revoke (not delete) keeps an audit trail for "which device
had this token when", which matters when a user asks why they got — or did
not get — a push.

### Rules

- **Register** (`PATCH /users/me/fcm-token`): UPSERT on `token` (unique
  constraint). A token moving to a new user implicitly revokes the old
  binding — write `revoked_at` on the old row + insert new. The current
  endpoint overwrites `users.fcm_token` in place
  ([app/routers/users.py:44-54](../app/routers/users.py#L44-L54)); the new
  one keeps the same URL and request shape, so the client needs no change.
  Platform comes from the SVC-255 `/users/me/push-capability` report
  ([app/schemas/user.py:28-41](../app/schemas/user.py#L28-L41)) or, more
  robustly, from a new `platform` field added to the FCM-token request
  schema itself — the client already knows it
  ([frontend/src/lib/fcm.ts:43](../../frontend/src/lib/fcm.ts)).
- **Send push**: fan-out across `revoked_at IS NULL` for the user. Hide
  the fan-out inside `fcm_client.send_to_user(user_id, …)` — see §3.
- **Logout** (`PATCH /users/me/fcm-token` with `null` body today, line 411
  in `frontend/src/lib/fcm.ts`): the logging-out client reports its token;
  backend sets `revoked_at` on that row only. Today's call sends the token
  as part of the clear-on-sign-out path — the client already has it in
  hand because `@capacitor/push-notifications` deletes it at FCM in the
  same step
  ([frontend/src/lib/fcm.ts:388-411](../../frontend/src/lib/fcm.ts#L388-L411)).
  Don't hard-delete the row — the point is history, not minimal state.
- **Dead-token clearing** (SVC-301 path at
  [app/tasks/notification_tasks.py:36-53](../app/tasks/notification_tasks.py#L36-L53)):
  on `UnregisteredError` or `SenderIdMismatchError`, set `revoked_at` on
  the one row that owns that token, not every row for the user. Today's
  code nulls `users.fcm_token` wholesale — that is why single-device is
  invariant-safe but is exactly what this fix changes.

### Migration path from `users.fcm_token`

- One alembic migration: create `device_tokens`, backfill every non-null
  `users.fcm_token` into it with `platform = 'android'` (every live beta
  user is Android — the frontend is Capacitor Android first, PWA
  secondary, and the SVC-255 capability log is the only way to confirm
  per user; a `platform = 'android'` default is safe because the FCM
  payload is identical across the three platforms for the pushes we send).
  `last_seen_at = users.updated_at` as the best-available proxy.
- Keep `users.fcm_token` as a nullable column for one release cycle. The
  send paths continue to read it as a fallback until the next migration
  drops the column — this keeps the deploy reversible for the duration of
  the cycle.
- The on-boot alembic in `docker-api-entrypoint.sh` runs this
  automatically on the first container start after merge
  ([CLAUDE.md § Migrations on beta](../CLAUDE.md)). PR carries the
  `db-migrate` label.

## 3. Impact on existing push paths

Every send-point today reads `recipient.fcm_token` directly. Full list,
from `grep -rn "send_push_notification\|send_multicast" app/`:

| Call site | Audience | Reads from | Fan-out needed? |
| --- | --- | --- | --- |
| [app/tasks/notification_tasks.py:648-667](../app/tasks/notification_tasks.py#L648) — `send_booking_notification` per-recipient loop | consumer / provider / Pro, per booking event (SVC-278, SVC-320, SVC-348) | `recipient.fcm_token` | Yes — the recipient could own more than one live device. |
| [app/tasks/notification_tasks.py:715-733](../app/tasks/notification_tasks.py#L715) — `send_payout_notification` | provider, per settlement | `provider.fcm_token` | Yes. |
| [app/tasks/notification_tasks.py:777-788](../app/tasks/notification_tasks.py#L777) — `broadcast_update_available` | all consumers with a token (SVC-227) | `SELECT User.fcm_token` scalar list | Yes — this is already a multicast; the shape stays, but the SELECT widens to `device_tokens.token WHERE revoked_at IS NULL` and may yield >1 row per user. |
| [app/services/notification_service.py:14-26](../app/services/notification_service.py#L14) — `NotificationService.send_push` | caller-provided token | arg | No change at the fn — but it has no callers today (`grep` shows import only); orthogonal to this spike. |
| [app/services/provider_matching.py:92](../app/services/provider_matching.py#L92) — `broadcast_recipients_stmt` filter | eligibility, not send | `User.fcm_token.is_not(None)` | **Yes, subtle:** the filter today excludes a provider whose single token is NULL. After migration it must become `EXISTS (SELECT 1 FROM device_tokens WHERE user_id = users.id AND revoked_at IS NULL)` — otherwise SVC-276's "reachable by push" guarantee silently fails for a provider mid-migration whose `users.fcm_token` was cleared but who has a live row in `device_tokens`. |

**Recommended shape for callers**: hide the fan-out behind the existing
`send_push_notification` or a new sibling `send_to_user(user_id, …)` in
[app/utils/fcm_client.py](../app/utils/fcm_client.py). Every current
caller already has `recipient.user_id` in hand (it's either the loop
variable or `provider.id`); switching from "token in, one send" to
"user_id in, N sends" is a 1-line change per call site, and the dead-token
loop at [notification_tasks.py:668-675](../app/tasks/notification_tasks.py#L668)
collapses from "null the one column" to "revoke the one row that failed".

**Expected count-of-changes**: 3 call sites (the first three rows), plus
`broadcast_recipients_stmt`, plus the register/clear service method, plus
`SVC-301`'s `_clear_dead_fcm_token` helper. ~6 small diffs behind one
migration; no public-API change.

**No call site today sends a push that only makes sense on the registering
device.** The nearest candidate — the SVC-350 Profile "Off" toggle at
[frontend/src/lib/fcm.ts:389-411](../../frontend/src/lib/fcm.ts#L389-L411)
— is a *clear*, not a push, so it stays unaffected.

## 4. Capacitor + web overlap — dedup strategy

A beta user who has both the native Capacitor Android build and the PWA
pinned to home-screen can register two tokens for the same account: one
from `@capacitor/push-notifications` on native, one from
`firebase/messaging` in the browser's service worker. Each is a *separate*
FCM registration token, so a fan-out send delivers two notifications for
one booking event — one through the native plugin, one through the SW.

Three options:

**(a) Trust the user.** Two notifications; let them turn off the one they
do not want through the OS / browser.

**(b) Platform preference at send time.** Backend knows `platform` per
token (§2). Send to native first; send to web only if no live native token
for the user.

**(c) Client-side dedup via a shared notification tag/id.** Both channels
would need to agree on a stable ID per booking event and the service
worker + native handler would need to replace rather than add.

**Recommend (a).** The data from §1 says we have one user with a token
and the overlap problem is zero in the observable population. (b) is a
one-line backend change *at the point we actually see overlap* and ships
cheaper than preemptively building (c), which touches both FCM payload
shape and the SW install contract (a schema break during a Vite
chunk-hash shift — see memory
[reference_vite_chunk_hash_busting](../.claude/memory/reference_vite_chunk_hash_busting.md)
analogue — is a one-day rabbit hole). Dedup is a classic rabbit hole:
every option that touches both ends of the stack has a long tail. If (a)
produces one real user complaint, pick up (b) in a dedicated ticket.

## 5. Follow-up ticket proposals

Three. Each small enough to spawn independently.

### 5.1 — Build `device_tokens` behind a flag (medium)

**Scope.** Alembic migration for the table in §2; backfill job in the
same migration; `send_to_user(user_id, …)` helper in `fcm_client.py`;
switch the 3 send-points in §3 to it; widen
`provider_matching.broadcast_recipients_stmt` to the EXISTS form. Keep
`users.fcm_token` populated in parallel for the release cycle so a
rollback is a one-line revert of the service method's write path.

**Non-goals.** Dedup (§4). Dropping the legacy column.

**Effort.** 2–3 days. One PR, `db-migrate` label, no public-API change.

**Priority.** Trigger-based: build only when a user complaint about
"missed on second device" lands, or at ≥100 providers onboarded. Not
sooner — the data says there is nothing to fix today.

### 5.2 — Drop `users.fcm_token` (small, pure cleanup)

**Scope.** After 5.1 has shipped for one release cycle without incident:
one migration dropping the column, one sweep removing fallback reads.

**Non-goals.** Any functional change.

**Effort.** ~1 day. One PR; schema change is the whole point of the label.

**Priority.** Low. Blocked on 5.1 + a successful release cycle. Dormant
columns cost nothing (per
[docs/pro-entity/HLD.md](pro-entity/HLD.md) on `parent_provider_id`), so
there is no urgency.

### 5.3 — Collect `platform` on `PATCH /users/me/fcm-token` (tiny)

**Scope.** Add a required `platform` field (`"android" | "ios" | "web"`)
to `UpdateFCMTokenRequest`
([app/schemas/user.py:65-70](../app/schemas/user.py#L65)); client sends
it from `Capacitor.getPlatform()` which it already reads in
[frontend/src/lib/fcm.ts:43](../../frontend/src/lib/fcm.ts). Store on
`users` as `fcm_platform` (nullable `TEXT`) if 5.1 is still deferred,
or on `device_tokens.platform` if 5.1 has shipped.

**Non-goals.** The `device_tokens` table.

**Effort.** 2–4 hours. Both apps.

**Priority.** Medium. Unblocks §4's option (b) if we ever need it,
removes the SVC-255 shape-mismatch where `platform` lives only in log
lines, and buys the first factual signal about iOS beta usage. Could be
done independently of 5.1.

## Found-during flags

Noticed while reading, flagged (not fixed) per the spike's "no code"
rule. None are blockers for 5.1.

1. **`fcm_token` column width = 255** at
   [app/models/user.py:27](../app/models/user.py#L27). The six live tokens
   in beta are all 142 chars, which is suspicious — FCM v1 Android tokens
   are typically 152–163. Either the beta sample happens to cluster at
   142, or something is truncating. Worth a one-off `LENGTH` audit after
   5.1 lands (`device_tokens.token` is `TEXT` with no cap, so the
   migration-time behaviour is to accept whatever the client sends).
2. **No raw-HTTP FCM senders.** Every send path goes through
   [app/utils/fcm_client.py](../app/utils/fcm_client.py); grep for
   `requests.post\|httpx.*fcm` returns zero hits. Safe.
3. **No admin test-push surface.** No "send a test to this one device"
   route in [app/routers/admin.py](../app/routers/admin.py). If one is
   ever added, it must take `token` (one row) not `user_id` (fan-out),
   or dedup/fan-out semantics get into the operator's hands.
4. **`NotificationService.send_push` has zero callers.** Imported but
   never invoked ([app/services/notification_service.py](../app/services/notification_service.py)).
   Not scoped here, but a candidate for either deletion or adoption.
