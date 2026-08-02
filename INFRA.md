# Infrastructure Runbook — Staging + Production (SVC-44)

How to provision, configure, and cut over the two AWS environments that the
Eazyserv backend runs on. Companion to [BACKUPS.md](BACKUPS.md) (which
covers RDS backups + restore drill) and [ADMIN.md](ADMIN.md) (which covers
the admin IP allowlist).

**TL;DR** — one AWS account, two VPCs (staging + prod), Mumbai region
(`ap-south-1`). Managed Postgres (RDS with PostGIS), managed Redis
(ElastiCache), containers on Fargate behind an ALB, KYC docs in a private S3
bucket. Everything is separate between environments — no shared DB, cache,
or credentials.

---

## The topology at a glance

```
┌──────────────────────── AWS Account (ap-south-1) ─────────────────────────┐
│                                                                            │
│  ┌── VPC: staging ─────────────────┐   ┌── VPC: production ────────────┐  │
│  │                                 │   │                               │  │
│  │  ALB (public)                   │   │  ALB (public)                 │  │
│  │   └─ Fargate: api  ×2 (0.5vCPU) │   │   └─ Fargate: api ×3 (1vCPU)  │  │
│  │   └─ Fargate: workers ×1        │   │   └─ Fargate: workers ×2      │  │
│  │                                 │   │                               │  │
│  │  RDS Postgres (PostGIS)         │   │  RDS Postgres (PostGIS)       │  │
│  │   - db.t4g.small, single-AZ     │   │   - db.t4g.medium, Multi-AZ   │  │
│  │   - 14-day PITR                 │   │   - 14-day PITR + snapshots   │  │
│  │                                 │   │                               │  │
│  │  ElastiCache Redis              │   │  ElastiCache Redis            │  │
│  │   - cache.t4g.micro             │   │   - cache.t4g.small, Multi-AZ │  │
│  │                                 │   │                               │  │
│  │  S3 bucket:                     │   │  S3 bucket:                   │  │
│  │   sc-staging-kyc                │   │   sc-prod-kyc                 │  │
│  │                                 │   │                               │  │
│  └─────────────────────────────────┘   └───────────────────────────────┘  │
│                                                                            │
│  Route 53:                                                                 │
│    api.staging.eazyserv.com    →    staging ALB                       │
│    api.eazyserv.com            →    prod ALB                          │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

Same account, different VPCs. IAM roles are scoped per-environment so a
staging Fargate task literally cannot reach the prod S3 bucket.

---

## Cost expectations

Based on the projections in [cost-estimates.html](../cost-estimates.html):

| Component | Staging | Production (Beta) |
|---|---|---|
| RDS Postgres | db.t4g.small — ~₹2,400 /mo | db.t4g.medium Multi-AZ — ~₹10,000 /mo |
| ElastiCache | cache.t4g.micro — ~₹1,100 /mo | cache.t4g.small Multi-AZ — ~₹4,200 /mo |
| Fargate (API + workers) | 3 tasks, small — ~₹2,100 /mo | 5 tasks, larger — ~₹8,300 /mo |
| ALB | ~₹1,600 /mo | ~₹2,900 /mo |
| S3 + data transfer | negligible | ~₹800 /mo |
| **Total /mo** | **~₹7,200** | **~₹26,200** |

12-month AWS Free Tier (if the account is new) covers staging entirely for
the first year. Production is billed from day one.

---

## Environment separation policy

**Non-negotiable:**

- No shared credentials between staging and prod. IAM roles differ; DB
  credentials differ; Razorpay keys differ (test vs live); FCM projects
  differ.
- No shared S3 buckets. Staging KYC docs never touch the prod bucket and
  vice versa.
- No shared Redis. Rate-limit counters and OTPs must not leak across
  environments.
- Staging can be reset (drop + recreate DB) without notice. Prod cannot.

**Acceptable to share:**

- The Razorpay merchant account (they support test/live modes on one account)
- The AWS account itself (isolation is at the VPC / IAM level)
- The Sentry organisation (different projects per env)

---

## First-time provisioning (staging then production)

Do this once per environment. Estimated time: 90 minutes staging, 2-3 hours
production (Multi-AZ takes longer).

### 1. VPC + subnets

- Create a VPC (10.10.0.0/16 for staging, 10.20.0.0/16 for prod)
- 2 public subnets (ALB), 2 private subnets (Fargate + RDS + Redis)
- NAT gateway in one AZ (staging) or two AZs (prod, HA)
- Internet gateway attached to the VPC

Console: VPC → "Create VPC and more" wizard covers all of this.

### 2. Security groups

Four SGs per VPC:

| SG | Ingress from | Purpose |
|---|---|---|
| `sc-{env}-alb-sg` | 0.0.0.0/0 :443 | ALB from public internet |
| `sc-{env}-api-sg` | alb-sg :8000 | Fargate API from ALB only |
| `sc-{env}-db-sg` | api-sg :5432 | RDS from API only |
| `sc-{env}-redis-sg` | api-sg :6379 | Redis from API only |

Neither RDS nor Redis SG has ingress from anywhere else — Fargate is the
only path.

### 3. RDS Postgres (with PostGIS)

**Staging:**
- Engine: PostgreSQL 16.x
- Instance: `db.t4g.small`
- Storage: 20 GB gp3
- Multi-AZ: **no** (cost)
- Deletion protection: yes
- Automated backups: 14 days
- Backup window: 21:30–22:30 UTC (03:00 IST off-peak)
- KMS encryption: default AWS-managed key
- Master user: `sc_admin`
- DB name: `eazyserv_staging`
- Security group: `sc-staging-db-sg`
- Subnet group: private subnets only
- Publicly accessible: **no**

**Production:**
- Same as above, but:
  - Instance: `db.t4g.medium`
  - Storage: 50 GB gp3 (with autoscaling to 200 GB enabled)
  - Multi-AZ: **yes**
  - Enhanced monitoring: 60s granularity
  - Performance Insights: enabled (7-day retention)
  - DB name: `eazyserv_prod`

**After provisioning — enable PostGIS:**

```sql
-- Connect via bastion or session-manager tunnel:
CREATE EXTENSION IF NOT EXISTS postgis;
SELECT postgis_version();  -- should return "3.x USE_GEOS=1 ..."
```

### 4. ElastiCache Redis

- Engine: Redis 7.x
- Node type: `cache.t4g.micro` (staging) / `cache.t4g.small` (prod)
- Number of replicas: 0 (staging) / 1 (prod, Multi-AZ)
- Automatic failover: on (prod only)
- Backup retention: 3 days (prod), 0 (staging)
- Subnet group: private subnets
- Security group: `sc-{env}-redis-sg`
- Encryption at rest + in transit: enabled

### 5. S3 bucket for KYC docs

**Staging:**
- Name: `sc-staging-kyc`
- Region: `ap-south-1`
- Block all public access: **on** (KYC docs are never public)
- Versioning: enabled
- Default encryption: SSE-KMS (default key)
- Lifecycle policy: none for staging

**Production:**
- Name: `sc-prod-kyc`
- Same settings, plus:
  - Lifecycle: transition to Glacier Deep Archive after 90 days (compliance retention)
  - Object Lock: governance mode, 7-year retention (DPDP audit trail)
  - Access logging to `sc-prod-audit-logs`

Bucket policy (both):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": ["arn:aws:s3:::sc-{env}-kyc/*"],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
```

### 6. Fargate cluster + task definition

- ECS cluster: `sc-{env}` (Fargate launch type)
- Task definition: 2 containers per task (api + envoy sidecar deferred; not needed at Beta)
- CPU / memory: 512 / 1024 (staging), 1024 / 2048 (prod)
- Task role: allows S3 PutObject/GetObject on `sc-{env}-kyc/*`, ElastiCache access, SSM ParamStore read
- Execution role: pulls image from ECR, writes CloudWatch logs
- Environment: injected from AWS SSM Parameter Store (see `.env.staging.example`)
- Health check: `curl -f http://localhost:8000/health || exit 1`
- Service: min 2, max 6 tasks; target CPU 60%

Separate task definition for the Celery worker (same image, different command:
`celery -A app.tasks.celery_app worker --loglevel=info`).

### 7. ALB

- Scheme: internet-facing
- Listener: 443 HTTPS with ACM cert for the environment's domain
- Target group: Fargate API service (health check `/health`)
- HTTP → HTTPS redirect on port 80
- WAF: skip for staging, enable AWS Managed Rules on prod

### 8. Route 53

- Hosted zone: `eazyserv.com`
- A/ALIAS records:
  - `api.staging.eazyserv.com` → staging ALB
  - `api.eazyserv.com` → prod ALB
- Health check on each ALB → SNS alert to ops on failure

### 9. Secrets in SSM Parameter Store

For each env, create `SecureString` parameters at `/sc/{env}/`:

```
/sc/staging/DATABASE_URL
/sc/staging/REDIS_URL
/sc/staging/JWT_SECRET
/sc/staging/RAZORPAY_KEY_ID
/sc/staging/RAZORPAY_KEY_SECRET
/sc/staging/RAZORPAY_WEBHOOK_SECRET
/sc/staging/S3_BUCKET_NAME
/sc/staging/FCM_CREDENTIALS_JSON     (the whole file, base64-encoded)
/sc/staging/ENCRYPTION_KEY
/sc/staging/ADMIN_ALLOWED_IPS
...same set at /sc/production/
```

Fargate task definition references these via `secretsFromSsm` — the container
sees them as env vars but they're never in an image or in Git.

---

## Deploy flow

Once provisioned, deploys follow this shape (CI/CD wiring in SC-45):

```
1. Push to main
2. GitHub Actions:
   - lint + typecheck + test
   - build Docker image, tag with commit SHA
   - push to ECR
3. Deploy to staging (automatic on main):
   - Run alembic upgrade head against staging RDS (release command)
   - Update Fargate service with new task revision
4. Manual approval gate → deploy to prod
   - Same shape, prod RDS + Fargate
   - Slack notification on start + finish
```

CI/CD itself is SC-45; this doc covers the infrastructure it deploys to.

---

## Environment env vars (see .env.staging.example, .env.production.example)

Both files check into the repo with **placeholder values only** — the real
secrets live in SSM. The example files exist so a new dev knows the shape
without having to spelunk config.py.

Key differences between staging and prod values:

| Var | Staging | Production |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` staging RDS | prod RDS |
| `REDIS_URL` | staging ElastiCache endpoint | prod ElastiCache |
| `RAZORPAY_KEY_ID` | `rzp_test_...` | `rzp_live_...` |
| `S3_BUCKET_NAME` | `sc-staging-kyc` | `sc-prod-kyc` |
| `FCM_CREDENTIALS_PATH` | staging FCM project | prod FCM project |
| `ADMIN_ALLOWED_IPS` | office egress | office + VPN only |
| `ADMIN_TRUST_FORWARDED_FOR` | `true` (behind ALB) | `true` |
| `SENTRY_DSN` (post-observability) | staging project | prod project |

---

## Restore-drill acknowledgement

The [BACKUPS.md](BACKUPS.md) restore drill uses staging as the target for the
quarterly (monthly-during-beta) restore. Never restore into prod as a drill —
the drill is designed to run against a throwaway environment. Staging IS
that throwaway.

---

## What's NOT in this runbook (deliberate)

- **CI/CD pipeline** — SC-45. This doc says "run alembic here", not "build
  a GitHub Actions workflow".
- **CDN / static asset hosting** — not applicable to the backend; the
  frontend PWA on Cloudflare Pages is separate.
- **Observability wiring** — SC-46. This doc mentions Sentry DSN as a
  placeholder for when SC-46 lands.
- **Terraform / CloudFormation** — the console + CLI walkthrough above is
  what beta needs. IaC comes when we have a real DevOps person; for now,
  human-readable steps are more useful than half-broken .tf files.

---

## Provisioning checklist

Copy this into a doc when you actually run it, tick as you go.

### Staging

- [ ] AWS account created / SSO configured
- [ ] VPC + subnets (10.10.0.0/16)
- [ ] Security groups (alb / api / db / redis)
- [ ] RDS Postgres db.t4g.small provisioned
- [ ] `CREATE EXTENSION postgis;` run + verified
- [ ] Alembic migrations applied (`uv run alembic upgrade head`)
- [ ] ElastiCache Redis cache.t4g.micro provisioned
- [ ] S3 bucket `sc-staging-kyc` with block-public-access
- [ ] ECR repo `sc-backend` created
- [ ] Fargate cluster `sc-staging` + task definitions
- [ ] ALB with ACM cert for `api.staging.eazyserv.com`
- [ ] Route 53 A record
- [ ] SSM parameters at `/sc/staging/*`
- [ ] Fargate service deployed, health check green
- [ ] `curl https://api.staging.eazyserv.com/health` returns 200
- [ ] `POST /api/v1/auth/send-otp` from a test phone reaches SMS provider

### Production

Same list, replace `staging` with `production`, plus:

- [ ] Multi-AZ enabled on RDS + Redis
- [ ] Deletion protection enabled on RDS
- [ ] AWS WAF managed rules on ALB
- [ ] CloudWatch alarms configured (RDS snapshot growth per BACKUPS.md)
- [ ] Sentry project created for prod (SC-46 will wire in)
- [ ] First backup snapshot taken + cross-region-copied to `ap-southeast-1`
- [ ] First restore drill performed on staging using latest prod snapshot

---

## Open items for SC-45 (CI/CD) to consume

- Fargate service names, task-definition families, ECR repo — CI needs these
  to push images and update services.
- SSM parameter paths — CI needs read access to inject secrets during deploy.
- ALB target group ARNs — CI needs these to trigger deployments.
- IAM role for the CI/CD OIDC provider (GitHub Actions).

All of these should be output as Terraform outputs / CloudFormation exports
once IaC lands post-beta.
