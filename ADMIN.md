# Admin Access Runbook (SVC-75)

`/admin/*` sits behind **two** layers of defence:

1. **JWT role check** (`require_role("admin")`) — enforced per-endpoint.
2. **IP allowlist middleware** (`AdminIPAllowlistMiddleware`) — enforced app-wide
   before the router. Optional; empty allowlist = no restriction (dev-safe).

Even a stolen admin JWT is useless from an unlisted network.

---

## Config

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
   curl -H "Authorization: Bearer <admin-jwt>" https://api.serviceconnect.in/api/v1/admin/providers/kyc/pending
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

- **Compromised admin machine on an allowed network.** Solved separately by
  Cloudflare Access / short JWT TTL / mandatory 2FA (all post-beta).
- **XSS / CSRF on the admin panel.** Different threat, handled at the
  frontend / CORS layer.
- **Insider abuse.** An admin with legit credentials from a legit network can
  still act maliciously. Audit logs (SVC-XX follow-up) are the answer here.

---

## Alternative implementations considered

- **Cloudflare Access (Zero Trust SSO)** — right answer long-term. Requires
  Cloudflare Zero Trust setup + user directory integration. Deferred to
  post-beta to keep the seed budget lean.
- **Google Workspace OIDC** — requires backend OAuth flow, admin login page.
  Bigger change; also deferred.

IP allowlist is the minimum-viable admin-hardening for beta — 2 SP, no new
vendor dependency, works today.
