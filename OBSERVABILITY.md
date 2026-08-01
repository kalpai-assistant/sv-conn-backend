# Observability Runbook (SC-47)

Log aggregation, alerts, and dashboards. Companion to [INFRA.md](INFRA.md)
(deploy topology), [BACKUPS.md](BACKUPS.md), and [SECRETS.md](SECRETS.md).
Complements the code-side observability work in SC-46 (structlog + Sentry
+ request-ID correlation) and SC-48 (health probes).

**TL;DR** — Better Stack Logs for aggregation (same vendor as Uptime → one
bill, one dashboard). Sentry for exceptions. Structlog already emits JSON
in prod (`LOG_JSON=true`); the log shipper picks that up unchanged. Alerts
covered in [§Alert catalog](#alert-catalog) — every alert has a runbook
link and a paging tier.

---

## Vendor decision

**Picked: Better Stack Logs.** Reasoning:

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Better Stack Logs** | Same vendor as Uptime (SC-48) — one login, one bill. Free tier covers ~1 GB/day, priced ~$25 per additional GB, fair for beta. HTTP + syslog ingest. Native log-query language. | Not Grafana-Loki-shaped — one-vendor lock-in. | ✅ **pick** |
| AWS CloudWatch Logs | Native to Fargate, "just works" via awslogs driver. | Query UX is painful. $0.50/GB ingest + $0.03/GB/month storage — cheaper than Better Stack but you pay in engineer-hours. Blocked on AWS account setup (SC-44). | Fallback if Better Stack pricing balloons. |
| Grafana Loki self-hosted | Cheapest at scale, own the data. | Requires EC2/EKS to run, ops burden we can't afford at 2-person team. | Post-beta option. |
| Datadog | Best UX. | ~$0.10 per log event ingested — kills the budget at any reasonable throughput. | No. |

**Cutover trigger:** revisit at 5 GB/day or 30 days post-launch, whichever
first. If we've hit the paid tier and are spending >$100/mo, migrate to
CloudWatch (assuming AWS is up by then).

---

## Log shipping architecture

Structlog already emits newline-delimited JSON to stdout in production
(`LOG_JSON=true`, see [`app/logging_config.py`](app/logging_config.py)).
Every record carries `timestamp`, `level`, `logger`, `event`, plus
`request_id` / `task_id` when in scope. Redaction (SC-50) is applied
before render, so tokens/OTPs/etc. never reach the shipper.

The Fargate task's stdout is routed to Better Stack via the AWS log
driver `awsfirelens` with a Fluent Bit output. **No app-side changes
needed** — this stays a deploy-time configuration in the task def.

### Fargate task definition snippet

Add this to every service's task def (api, workers, beat) — see
[`docs/task-def-logdriver.example.json`](docs/task-def-logdriver.example.json)
for the full block. Key parts:

```jsonc
{
  "logConfiguration": {
    "logDriver": "awsfirelens",
    "options": {
      "Name": "http",
      "Host": "in.logs.betterstack.com",
      "URI": "/",
      "Port": "443",
      "tls": "on",
      "tls.verify": "off",
      "format": "json",
      "header": "Authorization Bearer ${BETTERSTACK_LOGS_TOKEN}"
    }
  }
}
```

Sidecar Fluent Bit container in the same task def buffers on network
blips — see the referenced example for the exact spec.

### Why not the `awslogs` driver + CloudWatch subscription filter

Two hops (Fargate → CloudWatch → subscription → Better Stack) is one more
failure surface + one more bill (CloudWatch ingest fees) for zero gain.
Direct `awsfirelens` → Better Stack is simpler.

---

## Retention policy

| Log stream | Retention | Rationale |
|---|---|---|
| Application (api, workers, beat) | **14 days** | Long enough to correlate with a Friday-morning bug reported Monday. |
| Access logs (ALB) | **30 days** | Rare compliance/forensics ask (DPDP subject-access requests). |
| Audit logs (`admin_actions`, `payout_audit` tables) | **7 years** | DPDP + Razorpay financial audit — kept in DB, not in log stream. |
| Debug logs (LOG_LEVEL=DEBUG) | **48 hours** | Only enabled during active debugging; auto-flip back to INFO. |

Better Stack pricing is retention-driven, so shortening what we don't
need matters. 14 days is intentionally short — anything worth keeping
longer either is a Sentry event or belongs in a DB audit table.

---

## Alert catalog

Every alert below is defined once in Better Stack's alert config
(one-time click-through when the account is provisioned). This table is
the source of truth for what to fire, thresholds, and runbook.

**Paging tiers:**
- 🔴 **P1** — page primary on-call immediately (phone + Slack)
- 🟡 **P2** — Slack #ops, no phone, resolve within 2h
- 🔵 **P3** — Slack #ops, resolve within a business day

### Critical (P1)

| Alert | Query / signal | Runbook |
|---|---|---|
| **Payout reversed** | `event:payout.webhook status:reversed` — any occurrence | Provider's money was clawed back. Check RazorpayX dashboard for reason. If NEFT return, contact provider for updated bank details. |
| **HTTP 5xx spike** | 5xx rate > 10/min sustained 3 minutes | Check Sentry for exception trends. If backend down, verify `/healthz` on staging → check RDS + Redis. |
| **`/healthz` failure** | Any 503 from readiness probe (BetterStack Uptime already covers this) | LB is draining anyway; investigate which dep is down. See SC-48 body. |
| **DB connection exhaustion** | log matches `QueuePool limit .* reached` OR `remaining connection slots are reserved` | Restart api service; investigate slow queries via RDS Performance Insights. |
| **Alembic upgrade failure** | log matches `alembic.util.CommandError` in deploy stream | Deploy is halted mid-flight. Roll back task def to previous revision; do NOT retry until migration fixed. |

### High (P2)

| Alert | Query / signal | Runbook |
|---|---|---|
| **Payment stuck** | `event:payment.captured` without matching `booking.status:paid` within 5 min | Webhook received but reconciliation didn't apply. Check payment_service logs; if consistent, re-run `reconcile_payouts` task. |
| **Celery queue depth** | > 1000 tasks in any queue for > 5 minutes | Worker capacity issue OR broker down. Check worker pod count; scale up if legitimate load. |
| **OTP send failure spike** | log matches `SMS sending failed` > 20/min | MSG91 outage OR our key rotated. Check MSG91 dashboard, verify `MSG91_AUTH_KEY` in SSM. |
| **JWT signature failure spike** | log matches `Invalid or expired refresh token` > 50/min | Either key just rotated (expected — see SECRETS.md) OR credential-stuffing attempt. Cross-reference with source IPs. |
| **Rate limit exceeded — outlier** | Single IP triggers rate-limit > 100 times/hour | Likely abuse. Add to admin IP allowlist as EXPLICIT-DENY (SC-75 supports this). |

### Notice (P3)

| Alert | Query / signal | Runbook |
|---|---|---|
| **`Event loop is closed` recurrence** | log matches this exact string (defensive — we fixed one occurrence in the SC-45a follow-up; watching for regressions) | Bug in a redis/asyncpg fixture or dep upgrade. Bisect recent deploys. |
| **Structlog redaction miss** | log contains `rzp_live_[A-Za-z0-9]+` unredacted | A secret slipped through the value-pattern net. Add to `_SENSITIVE_KEY_SUBSTRINGS` or `_SENSITIVE_VALUE_PATTERNS` in `app/logging_config.py`. |
| **Reconcile unresolved** | `payout.reconcile` report field `unresolved > 0` | Human investigation needed — RazorpayX returned a payout our reconciler couldn't map to a batch. See PayoutService docstring. |

---

## Dashboards (what to create in Better Stack when account is live)

**Overview** (single screen for daily glances):

1. **Request rate** — line graph, 15 min buckets, by route
2. **Error rate** — 4xx and 5xx stacked, 15 min buckets
3. **P50/P95/P99 response time** — /bookings, /payments/verify, /admin/*
4. **Auth funnel** — send-otp count, verify-otp success, verify-otp failure
5. **Payment funnel** — orders created, verified, webhook received
6. **Celery queue depth** — one line per queue (default, notifications, payments)
7. **Redis + DB `/healthz` uptime** — 24h heatmap

**Operations** (during incidents):

8. **Log volume by service** — spot log storms
9. **Log volume by level** — INFO/WARNING/ERROR ratios
10. **Sentry event count** — with drill-through link to Sentry project

**Business KPIs** (weekly review, optional — depends on how much
non-log data we push):

11. Bookings/day
12. Payment success rate
13. Provider payout throughput

---

## Correlating Sentry ↔ Logs ↔ Traces

Every log record carries `request_id`. Sentry events tag the same
`request_id` via the SDK's `set_tag` — wired in
[`app/middleware/request_id.py`](app/middleware/request_id.py) once
Sentry integration lands in code (already installed as dep, just needs
the tag wire — see [§Follow-up code work](#follow-up-code-work)).

Investigation loop:
1. Alert fires in #ops (Better Stack) OR Sentry issue.
2. Grab the `request_id` from either surface.
3. Filter Better Stack logs by that ID → see full request lineage
   (middleware, service calls, task enqueue).
4. If the request enqueued a Celery task, filter by the same
   `request_id` — worker logs carry it via `task_prerun` signal (SC-46
   followup).

---

## Cost projection

Beta assumptions (from [cost-estimates.html](../cost-estimates.html)):
~2,000 users, 10 bookings/user/month, ~50 log events per booking flow
end-to-end.

- **~1 M events/month** at ~500 B/event = ~500 MB/month
- **Better Stack**: within the free tier (3 GB/month at time of writing).
- **Buffer for staging + spikes**: +100% → still under 1 GB/day.

Trigger to re-evaluate: > 3 GB/day sustained (would put us on paid tier
~$25/mo) OR > 30-day retention need (compliance edge case).

---

## Follow-up code work (small, tracked here to close the loop)

- [x] **Sentry `set_tag("request_id", ...)`** on every request — 5-line
      addition to `RequestIDMiddleware`. Wires SDK-side so Sentry issues
      cross-link to Better Stack log lines. **Shipped in this PR.**
- [x] **Sentry `set_user({id, role})` backend side** on `get_current_user`
      — Sentry can now filter events by user. `send_default_pii=False` at
      init time keeps phone/email out of Sentry. Frontend equivalent
      still open — see follow-up below. **Shipped in this PR.**
- [ ] **Frontend `Sentry.setUser`** in AuthContext on login/logout — small
      15 min task, tracked separately.
- [ ] **Prometheus `/metrics` endpoint** if we ever add Datadog/Grafana
      for numeric metrics. Deferred — Better Stack Logs covers most
      use cases via log-derived counts.

---

## What this runbook deliberately does NOT cover

- **APM tracing** (OpenTelemetry) — Sentry's `browserTracingIntegration`
  covers frontend; backend tracing is overkill at our scale. Add if we
  ever hit the "which sub-service is slow" problem, not before.
- **Real-user metrics** (RUM) — Sentry has a beta; skip until beta users
  actually complain about a specific screen.
- **Log-based billing** for provider ops (which admin approved which KYC
  when) — that's an audit table in Postgres, not the log stream. See
  `app/models/payout_audit.py` for the pattern.
