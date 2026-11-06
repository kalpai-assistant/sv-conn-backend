# Admin Access Runbook (SVC-75, SVC-422, SVC-423)

`/admin/*` sits behind **two** layers of defence:

1. **Login + role check** (`require_roles(...)`) — enforced per-endpoint.
2. **IP allowlist middleware** (`AdminIPAllowlistMiddleware`) — enforced app-wide
   before the router. Optional; empty allowlist = no restriction (dev-safe).

Even a stolen admin JWT is useless from an unlisted network.

---

## Admin console login (SVC-422)

Email + password, then a TOTP code. No self-signup. Admins are `users` rows with
`role=ADMIN`; `admin_role` holds `admin`, `ops` or `support`.

The phone login is retired: `POST /auth/admin/send-otp` and `verify-otp` return 410
`ADMIN_PHONE_LOGIN_GONE`, and old admin access and refresh tokens get 401.

| Endpoint | Does | Refusals |
|---|---|---|
| `POST /admin/auth/login` | Password step. Returns `requires_totp`, `totp_setup_required`, and a 5-minute `challenge_token`. | 401 `INVALID_CREDENTIALS`, 429 |
| `POST /admin/auth/totp/setup` | First login: returns `otpauth_url`; the app draws the QR. A new secret on every call. | 401 `INVALID_CHALLENGE`, 409 `TOTP_ALREADY_ACTIVE` |
| `POST /admin/auth/totp/verify` | Returns the session JWT, and activates TOTP on first success. | 401 `INVALID_CHALLENGE` / `INVALID_TOTP_CODE`, 409 `TOTP_SETUP_REQUIRED`, 429 |
| `POST /admin/auth/logout` | Ends the session (every token with its `sid`). | 401 |
| `GET /admin/auth/me` | Who is logged in. Console sessions only. | 401 |

**Limits.** Login: 5 attempts per IP per 15 minutes, using the rightmost
`X-Forwarded-For` entry, because a client controls the leftmost. This assumes one
proxy in front of the app; with more, all admins share one bucket. TOTP verify: 10
attempts per admin per 15 minutes. Three wrong passwords in a row lock the account
for 24 hours. Unknown email, wrong password, locked and deactivated accounts all
get the same 401.

**Session.** The JWT lasts 12 hours and has `typ=admin` and a `sid`; role and
`is_active` are read from the DB on every request. Past half its life, a response
carries a fresh token in `X-Admin-Token`, and the client should swap to it. There is
no absolute cap, so an active session can run indefinitely. Admin tokens carry
`aud=eazyserv-admin`, which makes every non-admin path refuse them.

### Add an admin

```bash
# An existing admin keeps their own row and history. Run this right after the
# deploy that adds the columns, once for every existing admin:
uv run python scripts/create_admin.py --email vipul@kalpai.in --phone +919999900001
# Someone new:
uv run python scripts/create_admin.py --email someone@kalpai.in --role ops
```

The password comes from `ADMIN_USER_PASSWORD` or a prompt, never an argument: 12+
characters, at most 72 bytes. On beta, run it through the SSM bastion tunnel in
CLAUDE.md. Nothing is seeded by the migration, so **nobody can log in until this has
run**. Someone new with no `--phone` gets a placeholder phone, `A` + 14 hex.

An admin who predates the migration can't be updated until the script has run for
them, because the CHECK holds every update to it. To list who still needs it:
`SELECT phone FROM users WHERE role = 'ADMIN' AND password_hash IS NULL;`. After the
last one, `ALTER TABLE users VALIDATE CONSTRAINT ck_users_admin_has_login;`.

### SQL recipes

```sql
-- Unlock an account
UPDATE users SET failed_login_count = 0, locked_until = NULL WHERE email = '...' AND role = 'ADMIN';
-- Deactivate (takes effect on the next request)
UPDATE users SET is_active = false WHERE email = '...' AND role = 'ADMIN';
-- Change role (takes effect on the next request)
UPDATE users SET admin_role = 'support' WHERE email = '...' AND role = 'ADMIN';
-- Lost authenticator: the next login goes through setup again
UPDATE users SET totp_secret = NULL, totp_enabled_at = NULL WHERE email = '...' AND role = 'ADMIN';
```

### Who can call what

A consumer or provider token gets 403; an old admin phone-login token or a challenge
token gets 401. `tests/test_admin_console_auth.py::ROUTE_ROLES` holds
this table and fails if a new `/admin` route isn't in it.

| Roles | Routes |
|---|---|
| admin, ops, support | `GET /admin/providers/kyc/pending`, `/providers/{id}/kyc`, `/pros/{id}/kyc`, `/bookings`, `/bookings/{id}`, `/bookings/{id}/timeline`, `/providers`, `/providers/{id}`, `/providers/{id}/team`, `/providers/{id}/invites`, `/pros/{id}` |
| admin, ops | `POST /admin/providers/{id}/kyc/verify` + `reject`, `POST /admin/pros/{id}/kyc/verify` + `reject`, `POST /admin/bookings/{id}/cancel`, `POST /services`, `PATCH` + `DELETE /services/{id}` |
| admin, support | `GET /admin/users`, `/actions`, `/providers/pending-settlements`, `/providers/{id}/payouts` |
| admin | `GET /admin/audit`, `/audit/filters`, `PATCH /admin/users/{id}/deactivate`, `POST /admin/providers/{id}/payout` + `requeue-failed` + `requeue-reversed`, `POST /admin/pros/{id}/force-remove` + `reassign`, `POST /admin/app-versions/{platform}/release` (also the CI token) |

Support has no write routes yet: its refund comes with SVC-431, and retry-dispatch
and booking-level reassign don't exist.

---

## Audit log (SVC-423)

Every admin write writes one `admin_actions` row: who, what, why, before and after,
IP and User-Agent. Rows are never updated or deleted, and there is no route that
could.

**Reading it.** `GET /admin/audit` (paginated, newest first; filter by
`admin_user_id`, `action`, `target_type`, `target_id`, `date_from` inclusive,
`date_to` exclusive; a date with no offset means UTC) and `GET /admin/audit/filters`.
Admin role only, because ops shouldn't review their own actions. `GET /admin/actions`
is the owner history on the Teams screen: admin and support, and only the 3 team
actions.

**Adding a write route.** Call the helper in the handler, in the same transaction as
the change, and add a case to `AUDITED_WRITE_ROUTES` in `tests/test_admin_audit.py`,
which fails until you do. Put a new action name in `app/models/admin_action.py`, in the
`area.verb` form. No migration is needed.

```python
before = snapshot("service", service)   # NULL for a create
...change it...
await audit_admin_action(
    db, audit_actor(request, admin),
    action="service.update", target_type="service", target_id=service.id,
    before=before, after=snapshot("service", service),   # NULL for a delete
)
```

**What can't be in a row.** `before_json` and `after_json` hold the short per-record
lists in `AUDIT_FIELDS`, never a whole record. These are dropped even if a caller
passes them: `password_hash`, `totp_secret`, `fcm_token`, every `*_encrypted` column,
`razorpayx_contact_id`, `razorpayx_fund_account_id`, `kyc_docs`, `kyc_ai_verdict`,
`doorstep_otp`.

**What each route writes.**

| Action | Target | Routes |
|---|---|---|
| `user_deactivated` | `user` | `PATCH /admin/users/{id}/deactivate` |
| `pro_force_removed`, `pro_reassigned` | `professional` | `POST /admin/pros/{id}/force-remove`, `reassign` |
| `kyc.approve`, `kyc.reject` | `provider` or `professional` | the 4 KYC verify and reject routes |
| `booking.force_cancel` | `booking` | `POST /admin/bookings/{id}/cancel` |
| `payout.enqueue` | `provider` | `POST /admin/providers/{id}/payout` |
| `payout.requeue_failed`, `payout.requeue_reversed` | `provider` | the 2 requeue routes |
| `app_version.release` | `app_version` (`target_id` is the platform) | `POST /admin/app-versions/{platform}/release` |
| `service.create`, `service.update`, `service.delete` | `service` | `POST`, `PATCH`, `DELETE /services` |

**Exceptions worth knowing.**

- **Payout.** One row when the route accepts (202), with the amount and booking count
  in `details`, committed before the task is enqueued. So a row means "an admin asked",
  not "money moved": a failed enqueue leaves a row and a 500, and an empty batch is a
  202 with a row showing 0. Each attempt's outcome stays in `payout_audits`. An unknown
  provider id is still a 202, with `sp_user_id` NULL.
- **CI release.** The token path writes no row, because no admin acted. Its record is
  the `app_version_published` log line with `release_auth=token`. A request sending
  both the token and a JWT counts as the token.
- **Actions that write no row.** Refused ones, and deactivating an unknown id (a silent
  204).

**IP and User-Agent.** Recording them never fails the action: an IP that doesn't parse
saves NULL, and the User-Agent is cut to 512 characters. The IP comes from
`appended_client_ip`, the same helper as the login limit. **Before the epic reaches
beta**, check the real `X-Forwarded-For` there and correct that one helper if needed
(see SVC-422's operational gaps). Audit rows only start on beta at that deploy.

**Downgrading.** The migration's downgrade deletes every row outside the 3 team
actions, because the old schema can't hold them. Copy the table first if they matter.
App Runner's auto-rollback redeploys the old image and never runs a downgrade.

---

## IP allowlist config

Two env vars in `.env`:

```bash
# Comma-separated CIDRs. Empty = no restriction (dev-safe default).
ADMIN_ALLOWED_IPS=""

# When the API sits behind a load balancer / CDN (ALB, Cloudflare), the real
# client IP arrives in X-Forwarded-For. Only enable this when the LB/CDN is
# known to strip inbound XFF headers — otherwise clients can spoof.
ADMIN_TRUST_FORWARDED_FOR=false
```

### Environment defaults

| Env | `ADMIN_ALLOWED_IPS` | `ADMIN_TRUST_FORWARDED_FOR` |
|---|---|---|
| Local dev | empty (no restriction) | `false` |
| Staging | office egress IPs, comma-separated | `true` (behind ALB) |
| Production | office IPs + VPN IPs; no home IPs unless emergency | `true` |

---

## Adding an admin's IP

1. Get the current egress IP from the admin's machine:
   ```bash
   curl ifconfig.me
   ```
2. Append to `ADMIN_ALLOWED_IPS` in the deploy env vars.
3. Redeploy (or `kubectl rollout restart` / equivalent).
4. Verify from the admin's machine:
   ```bash
   curl -H "Authorization: Bearer <admin-jwt>" https://api.eazyserv.com/api/v1/admin/providers/kyc/pending
   ```

**Don't add home IPs.** Home IPs churn (DHCP), so a next-day 403 wastes ops
time. Use the office network, or if a home admin is essential, put them
behind Cloudflare Access (post-beta upgrade).

---

## How it works

- Middleware runs **before** the router — a bad IP is rejected without ever
  touching admin route code or DB.
- Only paths starting with `${API_V1_PREFIX}/admin` are checked; everything
  else (bookings, health, docs) bypasses.
- Malformed CIDR entries are dropped at startup with an ERROR log rather than
  crashing the app — a typo shouldn't take non-admin traffic down.
- 403 responses are deliberately generic (`"Admin access is restricted from
  this network."`). We never leak which IPs are allowed.
- Every rejection is logged at WARNING with the offending IP, path, and
  user-agent — so ops can identify a genuine admin whose IP changed vs. a
  probe.

---

## What happens in each mode

### Dev (empty allowlist)

```
GET /api/v1/admin/kyc → 200 (no IP check; role check still applies)
```

### Production (allowlist set)

```
Admin from listed IP:   GET → 200 (both layers pass)
Admin from other IP:    GET → 403 (middleware blocks; role check never runs)
Non-admin JWT + listed IP: GET → 403 (IP passes, role fails at handler)
```

### Attacker with a leaked admin JWT

- From an unlisted network: 403 at the middleware; no way in short of
  compromising an allowlisted machine.
- From an allowlisted network: JWT works. This is a known limitation —
  compensating controls are session TTL (short JWT expiry) and log-based
  anomaly detection.

---

## What this does NOT protect against

- **Compromised admin machine on an allowed network.** Partly addressed by
  console TOTP (SVC-422). Cloudflare Access is still post-beta.
- **XSS / CSRF on the admin panel.** Different threat, handled at the
  frontend / CORS layer.
- **Insider abuse.** An admin with legit credentials from a legit network can
  still act maliciously. The audit log (SVC-423) records it but doesn't stop it.

---

## Alternative implementations considered

- **Cloudflare Access (Zero Trust SSO)** — right answer long-term. Requires
  Cloudflare Zero Trust setup + user directory integration. Deferred to
  post-beta to keep the seed budget lean.
- **Google Workspace OIDC** — requires backend OAuth flow, admin login page.
  Bigger change; also deferred.

IP allowlist is the minimum-viable admin-hardening for beta — 2 SP, no new
vendor dependency, works today.
