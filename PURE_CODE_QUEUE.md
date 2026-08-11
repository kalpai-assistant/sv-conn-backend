# Beta Work Queue

**Target:** Aug 9 launch (direct-APK for Android, PWA for iOS — no Play/App Store).
**Last updated:** 2026-08-02.

Everything below is a JIRA ticket. B/F/D shorthand from earlier drafts is
retired — SVC-XXX is the only naming convention. Delete this file the day
we launch.

---

## Critical path — beta blockers

### External (Vipul owns)

| Ticket | Status | Notes |
|---|---|---|
| — | ⏳ | **Provision AWS account.** Unblocks SVC-160 S3 bucket + SC-45b deploy pipeline + SC-47 dashboards. |
| — | ⏳ | **Create Firebase project** + download `google-services.json` + `GoogleService-Info.plist`. Unblocks SVC-156 → SVC-67 → SVC-40. |
| SVC-79 | ⏳ | Razorpay live-mode KYC. Check status; if not by Aug 6, launch with test-mode keys and cut over post-approval. |
| — | ⏳ | Register `eazyserv.com` DNS + point to Cloudflare + provision SSL cert. Blocks `app.eazyserv.com` + `api.eazyserv.com`. |
| — | ⏳ | **MSG91 SMS template** — submit "Your Eazyserv OTP..." template for approval (1-3 business days). Post-rename cosmetic; users see ServiceConnect until approved. |

### Distribution (new — Blinkit-tobacco pattern)

| Ticket | Priority | Owner | Depends on |
|---|---|---|---|
| **SVC-160** | Highest | Vipul (keystore) + Claude (build config) | AWS S3 bucket |
| **SVC-161** | Highest | Claude | SVC-160 (APK URL), DNS |
| **SVC-162** | High | Claude | SVC-160, SVC-161 |

### Provider flow end-to-end (SVC-40 chain — junior work)

| Ticket | Priority | Status | Notes |
|---|---|---|---|
| SVC-153 | ✅ | Merged | Offer endpoint |
| SVC-154 | ✅ | Merged | Accept error contract |
| SVC-156 | Highest | Vipul external | Firebase project + config files |
| SVC-67 | High | Junior | Blocked by SVC-156 |
| SVC-155 | Highest | Junior/Claude | Provider online/offline toggle (backend endpoint exists, needs frontend toggle + product decision on model) |
| SVC-40 | High | Junior | End-to-end card; blocked by SVC-67 + SVC-155 |

### Infra (Vipul-blocked but Claude-doable once unblocked)

| Ticket | Priority | Depends on |
|---|---|---|
| SC-45b (SVC-109) | High | AWS |
| SC-49 (SVC-113) | High | SVC-79 approved OR test-mode fallback |
| SC-47 (SVC-111) | Medium | Better Stack account (code + runbook done) |

### Beta hardening (Claude-doable now)

| Ticket | Priority | Notes |
|---|---|---|
| **SVC-157** | High | ⏳ **PR open, awaiting CI** — API contract tests |
| **SVC-158** | Medium | Idempotency-Key on POST /bookings |
| **SVC-159** | Low | Rate-limit `X-RateLimit-*` response headers |

---

## Explicitly CUT for Aug 9 beta

These are no longer in the critical path. Revisit post-beta.

- **SC-58 through SC-66** — All Play Store + App Store distribution. Replaced by SVC-160/161/162.
- **SC-71** Apple Developer org — not beta-blocking (iOS = PWA)
- **SC-72** Play Console — not beta-blocking (direct APK)
- **SC-74** APNs `.p8` — no iOS native push in beta
- **SC-42** Marketing screenshots — no store listings
- **SC-53** Final UX pass — real users are the QA
- **SC-55** Bug bash — same
- **SC-136** (PAY-7 v1.1 refactor) — already closed
- **Frontend polish nice-to-haves** — apiFetch retry, offline banner, loading skeletons, feature-flag helper, code-splitting admin routes
- **Backend nice-to-haves** — `app.state.redis` refactor, currency helper, Celery health probe, webhook JSON-schema tightening, fixture consolidation
- **Docs nice-to-haves** — CONTRIBUTING.md, rollback runbook, on-call playbook stub

---

## Sprint status snapshot

Rough count from the JIRA sweep on 2026-08-02:

- **Done:** SC-34, SC-35, SC-37, SC-44, SC-45a, SC-46 (+ Celery followup), SC-48, SC-50, SC-52, SC-67 (native config), SC-68, SC-69, SC-75, SC-90 (KYC review page)
- **In flight (code done, external gate):** SC-47 (SVC-111)
- **Blocked on Vipul externals:** SC-45b, SC-49, SC-71, SC-72, SC-73 (SVC-79), SC-74
- **Junior queue:** SVC-40 chain, SC-38, SC-41 (polish), plus rename PRs to review
- **New beta work:** SVC-155, SVC-156, SVC-157, SVC-158, SVC-159, SVC-160, SVC-161, SVC-162

---

## What I'm working on right now (Claude)

- ✅ Rename ServiceConnect → Eazyserv (PRs open, both repos)
- ✅ Filed SVC-160, SVC-161, SVC-162
- Next: pick up **SVC-161** (`/download` page) — biggest user-visible deliverable; can build the UI + backend endpoint without waiting on AWS. APK URL will be a `todo` placeholder until SVC-160's S3 bucket is up.

## What Vipul is working on

- AWS account provisioning (unblocks everything infra)
- Firebase project (unblocks SVC-156 → SVC-67 → SVC-40)
- Razorpay KYC status check
- Domain registration + DNS (eazyserv.com)
- Keystore generation for SVC-160
