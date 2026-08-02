# Secrets Hygiene & Rotation Runbook (SC-50)

Operational procedure for every secret the Eazyserv backend depends on.
Covers where each lives, how to rotate it, and what to do when one leaks.
Companion to [INFRA.md](INFRA.md) (which established SSM Parameter Store as the
backing store) and [ADMIN.md](ADMIN.md).

**TL;DR** — Every secret lives in SSM at `/sc/{env}/*`, never in Git, never in
Slack. Rotate on the schedule below or **immediately** on compromise. If a
secret is suspected leaked, jump to [Incident response](#incident-response).

---

## Inventory — every secret we depend on

Grouped by rotation urgency. "Never" means "not on a schedule, but rotate on
compromise or personnel change."

| Secret | Env var | SSM path | Rotate | Blast radius on leak |
|---|---|---|---|---|
| **JWT signing key** | `JWT_SECRET_KEY` | `/sc/{env}/JWT_SECRET` | **Quarterly** + on incident | Every session token forgeable → full user impersonation. Highest severity. |
| **AES-256 PII key** | `ENCRYPTION_KEY` | `/sc/{env}/ENCRYPTION_KEY` | **Never routinely** (see below) | Aadhaar / PAN plaintext derivable. DPDP-reportable. |
| **DB password** | `DATABASE_URL`, `SYNC_DATABASE_URL` | `/sc/{env}/DATABASE_URL` | **Quarterly** + on incident | Full DB read/write. Prod = every user's PII, bookings, payments. |
| **Redis auth** | `REDIS_URL` | `/sc/{env}/REDIS_URL` | Quarterly | OTPs, rate-limit counters, session cache. Low PII, high nuisance. |
| **Razorpay live keys** | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | `/sc/prod/RAZORPAY_*` | **On personnel change** | Attacker can initiate refunds, read payment history via API. Money movement possible. |
| **Razorpay webhook secret** | `RAZORPAY_WEBHOOK_SECRET` | `/sc/{env}/RAZORPAY_WEBHOOK_SECRET` | Annually + on incident | Attacker can forge payment webhooks (mark bookings paid). |
| **RazorpayX keys** | `RAZORPAYX_*` | `/sc/prod/RAZORPAYX_*` | **On personnel change** | Provider payouts — attacker can trigger real money out of the account. |
| **MSG91 API key** | `MSG91_AUTH_KEY` | `/sc/{env}/MSG91_AUTH_KEY` | Annually + on incident | Send SMS on our credit. Financial-only, no PII. |
| **FCM service-account JSON** | `FCM_CREDENTIALS_PATH` (points at file), file contents at `/sc/{env}/FCM_CREDENTIALS_JSON` (base64) | See INFRA.md §9 | Annually + on incident | Push notifications to our user base. Social-engineering vector. |
| **S3 access keys** | Should be IAM role, not keys — see below | — | N/A (role-based) | KYC docs (highest PII sensitivity). |
| **AWS root account** | Console credentials | 1Password, MFA-only | **Never used routinely** — break-glass only | Full AWS account takeover. Cannot recover. |
| **Sentry DSN** | `SENTRY_DSN` | `/sc/{env}/SENTRY_DSN` | On project reset | Attacker can flood our Sentry quota. No PII. |

---

## Where secrets live (and where they must NOT)

**Live:**
- **SSM Parameter Store** at `/sc/staging/*` and `/sc/prod/*`, all as
  `SecureString` with the default account KMS key. Read by Fargate task IAM
  role at container start.
- **1Password** (team vault: "Eazyserv ops") for anything a human ever
  needs to see — AWS root creds, Razorpay dashboard login, MSG91 dashboard
  login, Sentry admin login.
- **Developer laptops** in `.env` — dev keys only, never prod / staging.

**Must not be in:**
- Git (including `.env` — that's why `.env` is in `.gitignore` and
  `.env.example` only has placeholders).
- Slack / email / Notion / any chat log.
- Container images (CI/CD reads from SSM at deploy time; images stay clean).
- Application logs. Note: the structlog processor at
  `app/logging_config.py::_redact_secrets` catches known-sensitive keys
  (`password`, `token`, `otp`, `secret`, `authorization`, ...) and known
  patterns (JWTs, `rzp_live_*`, `AKIA*`) as a safety net. **Do not rely
  on it** — an `f"OTP is {otp}"` inside a log message body will still leak
  because we can only redact structured fields. Treat every log call
  touching a secret as a code-review flag.
- Screenshots, screen recordings, presentations.

If you catch a secret in any of the above during code review, treat it as
compromised — don't just delete, rotate.

---

## Rotation procedures

### JWT signing key (`JWT_SECRET_KEY`)

**Cadence:** Quarterly. Immediately on compromise or lead-engineer departure.

**Impact:** All existing access + refresh tokens invalidate. Every user is
logged out and must re-verify OTP.

```bash
# 1. Generate a fresh key (32 random bytes, base64):
NEW_KEY=$(openssl rand -base64 32)

# 2. Push to SSM (prod shown; run separately for staging):
aws ssm put-parameter \
  --name /sc/prod/JWT_SECRET \
  --value "$NEW_KEY" \
  --type SecureString \
  --overwrite \
  --region ap-south-1

# 3. Force a new Fargate task revision so containers pick it up:
aws ecs update-service \
  --cluster sc-prod \
  --service sc-api \
  --force-new-deployment \
  --region ap-south-1

# 4. Monitor for the wave of 401s (users re-authenticating) — should
#    settle within 30 min as apps auto-refresh via the OTP flow.
```

**Rollback if things go sideways:** put the old key back via `put-parameter`
and force-deploy. Existing sessions from before the rotation will validate
again.

**Announce** in #ops before starting — support gets a spike of "I got logged
out" tickets.

---

### AES-256 PII encryption key (`ENCRYPTION_KEY`)

**Cadence:** **Do not rotate on a schedule.** This key protects Aadhaar and
PAN at rest — rotating it means decrypting-and-re-encrypting every KYC row.
Batched migration, tested on staging first, run during a maintenance window.

**Rotate only when:**
- Key material is suspected leaked (Git commit, laptop theft, contractor
  access).
- DPDP requires it (audit finding).

**Procedure:**

1. Provision a new key alongside the old (add `ENCRYPTION_KEY_NEXT` env var).
2. Deploy a service update that reads from both keys but writes with the new
   one (two-key window).
3. Run `scripts/reencrypt_pii.py` to migrate all existing rows. Log
   row-by-row; abort on any failure.
4. Once migration is verified 100% clean, remove the old key and promote
   `ENCRYPTION_KEY_NEXT` → `ENCRYPTION_KEY`.

Script `scripts/reencrypt_pii.py` doesn't exist yet — write it on the day
you need to rotate. Sketch it during the incident, don't pre-build.

---

### DB password (`DATABASE_URL`)

**Cadence:** Quarterly.

RDS supports rotation via the console. The URL in SSM has to change to match:

```bash
# 1. Rotate at RDS:
aws rds modify-db-instance \
  --db-instance-identifier sc-prod \
  --master-user-password "$(openssl rand -base64 24 | tr -d '=+/')" \
  --apply-immediately \
  --region ap-south-1

# 2. Grab the new password (you set it — save to 1Password immediately).

# 3. Update SSM (both async + sync URLs — they share the password):
aws ssm put-parameter --name /sc/prod/DATABASE_URL \
  --value "postgresql+asyncpg://sc_user:$NEW_PW@sc-prod.xxxx.ap-south-1.rds.amazonaws.com:5432/eazyserv" \
  --type SecureString --overwrite --region ap-south-1

aws ssm put-parameter --name /sc/prod/SYNC_DATABASE_URL \
  --value "postgresql+psycopg2://sc_user:$NEW_PW@sc-prod.xxxx.ap-south-1.rds.amazonaws.com:5432/eazyserv" \
  --type SecureString --overwrite --region ap-south-1

# 4. Force-deploy Fargate:
aws ecs update-service --cluster sc-prod --service sc-api \
  --force-new-deployment --region ap-south-1
aws ecs update-service --cluster sc-prod --service sc-workers \
  --force-new-deployment --region ap-south-1
```

**Zero-downtime tip:** brief connection churn during the RDS password swap
is expected — user-facing 5xx spike for ~30s. Announce beforehand.

---

### Razorpay live keys (`RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`)

**Cadence:** On personnel change (engineer or admin with Razorpay dashboard
access leaves), or on compromise.

1. Log in to Razorpay dashboard as owner.
2. Settings → API Keys → Generate New Live Key. Old key stays active for 30
   days (Razorpay's grace window).
3. Push the new pair to SSM (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`).
4. Force-deploy Fargate.
5. **Confirm success** with one live test payment before revoking the old
   key. If broken, roll back SSM to the old key and investigate.
6. Return to the dashboard → revoke the old key.

Do not delete the old key without confirming the new one works — Razorpay
has no undo.

---

### Razorpay webhook secret (`RAZORPAY_WEBHOOK_SECRET`)

**Cadence:** Annually or on incident.

1. In Razorpay dashboard → Webhooks → Edit → generate new secret. Copy.
2. Push to SSM, force-deploy Fargate.
3. Send a test webhook via the dashboard — should return 200. If 400, the
   deploy didn't pick up the new secret yet; wait 60s and retry.

Razorpay doesn't have a two-secret window here — there's a ~30s cutover
where webhooks in flight will 400. Acceptable; Razorpay retries.

---

### FCM service-account JSON

**Cadence:** Annually or on compromise.

1. Firebase console → Project Settings → Service accounts → Generate new
   private key. Downloads a JSON file.
2. Base64-encode and push to SSM:
   ```bash
   base64 -i firebase-adminsdk-*.json | aws ssm put-parameter \
     --name /sc/prod/FCM_CREDENTIALS_JSON \
     --value file:///dev/stdin \
     --type SecureString --overwrite --region ap-south-1
   ```
3. Delete the local JSON file (`shred -u firebase-adminsdk-*.json`).
4. Force-deploy Fargate.
5. Revoke the previous key from the Firebase console (Actions → Delete).

---

### MSG91 API key

**Cadence:** Annually or on incident.

1. MSG91 dashboard → API Keys → generate new. Old key survives until
   deleted.
2. Push to SSM as `/sc/{env}/MSG91_AUTH_KEY`. Force-deploy.
3. Send one OTP via staging as a smoke test.
4. Delete the old key.

---

### AWS access keys (should not exist — use IAM roles)

**Do not create long-lived AWS access keys for the running service.** Fargate
tasks use an IAM role that assumes credentials automatically. The only
long-lived AWS credentials that should exist are:

- **CI/CD deploy role** — GitHub Actions OIDC → assumes an IAM role. No
  static keys stored in GH Secrets.
- **Break-glass admin** — one IAM user, MFA-required, in 1Password. Never
  used for routine work.

If you find an `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in SSM or a
task definition, that's a bug — file a security ticket to swap for a role.

---

## Access hygiene

- **SSM parameter access is scoped by IAM.** The Fargate task role has
  `ssm:GetParameters` for `/sc/{env}/*` only (not the sibling env). An
  engineer's console role has `ssm:GetParametersByPath` for troubleshooting
  but writes require the ops role.
- **Two-person rule for prod secret writes** is aspirational — enforce
  socially, review each `put-parameter` on prod in #ops.
- **Console logins:** all humans in the AWS account use SSO + MFA. Root
  account has hardware MFA and is used only for break-glass.
- **1Password vault "Eazyserv ops"** — membership matches the
  on-call rota. Remove departing engineers within 24 hours of last day.

---

## Incident response

You suspect a secret has leaked. Whether it's a Git commit that briefly
included a `.env`, a screenshot posted to Slack, or a laptop stolen —
treat as compromised and follow this order:

### Step 1: Contain (first 15 minutes)

Rotate immediately using the procedures above, in this priority order (most
damaging first):

1. **AWS root credentials** → change password, rotate MFA device, force sign
   out all sessions.
2. **RazorpayX + Razorpay live keys** → real money movement possible.
3. **DB password + JWT signing key** → data exfiltration + session theft.
4. **AES PII key** → follow the special procedure above.
5. **Everything else** in parallel.

### Step 2: Assess (first hour)

- Pull the last 24h of `payment_service` + `payout_service` logs from
  CloudWatch. Anything unexpected?
- Check RDS `pg_stat_activity` for suspicious sessions.
- Check S3 CloudTrail for any KYC-bucket access outside our known IPs.
- If the compromised secret was JWT, count the sessions that were active
  when it leaked — those are the users to notify.

### Step 3: Notify (first 24 hours)

- **DPDP disclosure clock starts** the moment you conclude PII was
  potentially accessed. 72-hour window to notify the Data Protection
  Board.
- **Affected users** get an in-app notice + SMS if the breach touched
  their data.
- **Razorpay + FCM** — inform their support channels if payment/notification
  infra was involved; they may have complementary logging.

### Step 4: Post-mortem

Blameless write-up within 5 business days. Should include:
- What leaked (which secret, from which surface).
- Root cause (why it landed there — process gap, tooling gap, human error?).
- Fixes: process (e.g., pre-commit hook for secret patterns), tooling
  (e.g., `git-secrets` in CI), documentation.
- Timeline: minute-by-minute for containment, coarser after.

Store in `docs/post-mortems/YYYY-MM-DD-<slug>.md` (create the directory on
first incident — no template, prose is fine).

---

## Preventive controls (do these once)

- [ ] `git-secrets` or `gitleaks` as a pre-commit hook in both repos.
- [ ] CI job in `sv-conn-backend` that greps every diff for common secret
      patterns (AWS keys, Razorpay `rzp_live_`, private key headers). Fails
      the PR on any hit.
- [ ] Enable AWS Config rule `SecretsManagerRotationEnabled` (even though
      we use SSM, similar controls apply — set up `ParameterStoreEncryption`
      compliance check).
- [ ] Quarterly access review: audit membership of AWS SSO groups,
      1Password vault, Razorpay dashboard, MSG91 dashboard, Firebase.

None of these are prerequisites for beta launch, but all should be in place
within 60 days of production go-live.

---

## What this runbook deliberately does NOT cover

- **AWS Secrets Manager** — we picked SSM Parameter Store (see INFRA.md
  §9 for the reasoning: cheaper, integrates with Fargate task defs the
  same way, and rotation via Lambda is overkill at our scale).
- **HashiCorp Vault / Doppler / etc.** — evaluated post-beta if the SSM
  surface starts to strain (>100 secrets, or multi-region).
- **Certificate rotation** — ACM auto-renews the ALB certs; no manual step
  needed. If ACM ever fails a renewal, AWS emails the account owner 45
  days out.
- **SSH keys** — no bastion, no EC2. Fargate + SSM Session Manager for
  container shell access; keys aren't in the loop.
