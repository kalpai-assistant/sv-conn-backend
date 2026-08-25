# AWS setup + IaC runbook

Everything Claude needs to bring up + operate the beta backend
autonomously, plus the migration path from click-ops to full
Terraform.

**Region: `ap-south-1` (Mumbai).** Not us-east-1 — the ~250ms extra
RTT to Bangalore makes the app feel laggy on every tap, and the ~$4/mo
cost delta at beta scale is meaningless.

**Stack decisions (locked in for beta):**

| Layer | Choice | Cost/mo | Why |
|---|---|---|---|
| Compute | AWS App Runner | $5 | Autoscales, HTTPS out of box, zero infra config |
| Postgres | Neon (managed, external) | $0 free tier | PostGIS on free tier; no `db.t4g.micro` minimum spend |
| Redis | Upstash Serverless | $0 free tier | Vipul's pick — quick, no ops |
| Registry | AWS ECR | $0 (< 500 MB) | Feeds App Runner |
| Secrets | AWS SSM Parameter Store | $0 | Per `SECRETS.md`; not Secrets Manager (that's $0.40/param/mo) |
| Object storage | AWS S3 | ~$0.10 | APK for SVC-160, DPDP export bucket later |
| Logs / metrics | CloudWatch | $0 free tier | Familiar; auto-integrates with App Runner |
| DNS | Cloudflare (already used for Pages) | $0 | Domain owned; api.eazyserv.com → CNAME to App Runner |
| Backend Sentry | reuse frontend Sentry org | $0 | Second project inside same org |

**Total beta run rate: ~$5/mo** (App Runner alone; everything else free
tier). Sub-$20 even if we blow through free tiers on FCM push volume.

---

## Part 1 — Access setup (one-time, Vipul does this once)

### 1.1 Create IAM user for Claude / CI

Console → IAM → Users → Create user.

- Name: `eazyserv-claude-beta`
- **AdministratorAccess** policy attached (bootstrap phase; we tighten
  once infra is stable — see §7)
- Console access: NOT needed (CLI-only user is safer)
- Create user

### 1.2 Access key

User → Security credentials tab → Create access key → CLI → tick
"I understand" → Create.

**Store IMMEDIATELY in 1Password** — the secret is shown once. Filename:
`AWS - eazyserv beta claude`.

### 1.3 CLI profile on Vipul's machine

```bash
aws configure --profile eazyserv
# access key: paste
# secret:     paste
# region:     ap-south-1
# output:     json
```

Verify:

```bash
aws sts get-caller-identity --profile eazyserv
# → {"UserId": "...", "Account": "...", "Arn": "arn:aws:iam::.../eazyserv-claude-beta"}
```

### 1.4 Handing to Claude

Tell Claude: **"AWS profile `eazyserv` is set, region ap-south-1,
account $ACCOUNT_ID."** Claude runs every `aws` call with
`--profile eazyserv` and never touches credentials directly.

---

## Part 2 — Manual setup order (bootstrap; §5 replaces this with Terraform)

Do these in order. Each is self-contained; you can pause between and
resume without breaking anything. Every command uses
`--profile eazyserv --region ap-south-1` (elided below for readability;
export `AWS_PROFILE=eazyserv AWS_DEFAULT_REGION=ap-south-1` first).

### 2.1 Neon Postgres

External to AWS — do this first because App Runner needs the URL.

1. https://neon.tech/ → sign up with GitHub → new project name `eazyserv-beta`
2. Region: **AWS ap-south-1** (Neon supports it; keep DB co-located)
3. Postgres version: 16
4. Enable extensions: **postgis** (Neon console → SQL editor →
   `CREATE EXTENSION postgis;`)
5. Copy the **pooled** connection string (has `-pooler` in hostname).
   Store as `DATABASE_URL` in SSM (§2.4).

Sanity check:

```bash
psql "$NEON_DATABASE_URL" -c "SELECT PostGIS_Version();"
```

### 2.2 Upstash Redis

1. https://upstash.com/ → sign up → new database name `eazyserv-beta`
2. Region: **ap-south-1** (aws-mumbai)
3. TLS: enabled
4. Copy the **Redis URL** (starts with `rediss://`). Store as
   `REDIS_URL` in SSM.

### 2.3 SSM Parameter Store (secrets)

Populate every secret under `/eazyserv/beta/*`. All `SecureString`
type — free, KMS-encrypted with the default AWS-managed key.

```bash
put() {
  aws ssm put-parameter --name "/eazyserv/beta/$1" --value "$2" \
      --type SecureString --overwrite
}

put DATABASE_URL         '<neon-pooled-url>'
put SYNC_DATABASE_URL    '<neon-pooled-url, but psycopg2:// scheme>'
put REDIS_URL            '<upstash-rediss-url>'
put JWT_SECRET_KEY       "$(openssl rand -base64 32)"
put ENCRYPTION_KEY       "$(openssl rand -base64 32)"  # must be 44 chars base64
put RAZORPAY_KEY_ID      '<from razorpay dashboard>'
put RAZORPAY_KEY_SECRET  '<from razorpay dashboard>'
put RAZORPAY_WEBHOOK_SECRET  '<generated on razorpay webhook setup>'
put FCM_CREDENTIALS_JSON "$(base64 -i path/to/fcm-service-account.json)"
put MSG91_AUTH_KEY       '<from msg91 dashboard>'
put MSG91_TEMPLATE_ID    '<DLT-approved template id from msg91 → SMS → Templates>'
put GOOGLE_MAPS_API_KEY  '<from gcp console>'
put ADMIN_ALLOWED_IPS    '<home ip>/32'  # tighten later
```

Full parameter list → `SECRETS.md`. Every value gets checked at startup
by `app/utils/encryption.py`'s validation (SVC-audit fix, PR #58).

### 2.4 ECR (container registry)

```bash
aws ecr create-repository --repository-name eazyserv-backend \
    --image-scanning-configuration scanOnPush=true

# Login (creds valid 12h)
aws ecr get-login-password | docker login --username AWS \
    --password-stdin $ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com

# Build + push
docker build -t eazyserv-backend .
docker tag eazyserv-backend:latest \
    $ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/eazyserv-backend:beta
docker push $ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/eazyserv-backend:beta
```

### 2.5 Alembic migrations against Neon

Before creating the App Runner service. If migrations fail, we don't
want a live service pointing at an unmigrated DB.

```bash
export DATABASE_URL=<neon-pooled-url>
uv run alembic upgrade head
uv run alembic check  # confirms no drift
```

### 2.6 App Runner service

Cannot easily do this end-to-end via CLI in a docstring — the
CLI needs a JSON config blob. Console is faster for the first bring-up.

Console → App Runner → Create service:

- **Source**: Container registry → Amazon ECR → the image pushed in §2.4
- **Deployment**: Automatic (App Runner watches ECR for `:beta` tag)
- **ECR access role**: Create new service role (Console offers to make
  it — `AppRunnerECRAccessRole`)
- **Service name**: `eazyserv-backend`
- **Virtual CPU & memory**: **0.25 vCPU / 0.5 GB** (the floor; $5/mo)
- **Environment variables**: pull from SSM using the "Fetch from SSM"
  option — set every key we `put` in §2.3 with source SSM Parameter.
  App Runner reads them on startup + on secret rotation.
- **Port**: `8000` (uvicorn default we use in Dockerfile)
- **Health check**: HTTP path `/health` (SVC-48 liveness), interval 20s,
  timeout 5s, healthy 1, unhealthy 3
- **Auto scaling**: default config (1–10 instances, 100 concurrency)
- **Security**: default (public endpoint)

Wait ~5 min for first deploy. Note the App Runner URL —
`https://xxx.ap-south-1.awsapprunner.com`. Curl `/health` — should
return 200.

### 2.7 S3 bucket for APK (closes SVC-160 back half)

```bash
aws s3api create-bucket --bucket eazyserv-downloads \
    --create-bucket-configuration LocationConstraint=ap-south-1

# Public read on /apk/* only. NOT the whole bucket.
aws s3api put-public-access-block --bucket eazyserv-downloads \
    --public-access-block-configuration \
    BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false

aws s3api put-bucket-policy --bucket eazyserv-downloads --policy '{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicReadApk",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::eazyserv-downloads/apk/*"
  }]
}'
```

Upload the signed APK from `RELEASE.md` step 4:

```bash
aws s3 cp app-release.apk s3://eazyserv-downloads/apk/eazyserv-1.0.0.apk \
    --cache-control 'public, max-age=3600'
aws s3 cp app-release.apk s3://eazyserv-downloads/apk/latest.apk \
    --cache-control 'no-cache'
```

Set `VITE_APK_URL` in Cloudflare Pages to
`https://eazyserv-downloads.s3.ap-south-1.amazonaws.com/apk/latest.apk`
→ redeploy → the /download button goes live.

### 2.8 Cloudflare DNS: `api.eazyserv.com` → App Runner

Cloudflare dashboard → eazyserv.com → DNS → Add record:

- Type: `CNAME`
- Name: `api`
- Target: `xxx.ap-south-1.awsapprunner.com` (the App Runner URL)
- Proxy status: **DNS only** (grey cloud — App Runner already does
  HTTPS termination; proxying through Cloudflare adds a hop and can
  break long-poll / WebSocket for SVC-tracking)
- TTL: Auto

Custom domain on App Runner:

Console → App Runner → your service → Custom domains → Link domain →
`api.eazyserv.com` → follow the CNAME verification (Cloudflare needs
one additional TXT record). Wait ~10 min for TLS cert provisioning.

Verify:

```bash
curl -i https://api.eazyserv.com/health
```

### 2.9 Point frontend at the new API

Cloudflare Pages → sv-conn-frontend → Settings → Environment variables
→ Production:

- `VITE_API_BASE_URL` = `https://api.eazyserv.com/api/v1`

Deployments tab → Retry deployment. Frontend now talks to the deployed
backend.

### 2.10 Backend Sentry (~2 min)

- Sentry → existing eazyserv org → Create project → **Python** platform
- Project name: `eazyserv-backend`
- Copy DSN → SSM `put SENTRY_DSN '<dsn>'`
- App Runner service → env vars → add `SENTRY_DSN` from SSM → deploy

### 2.11 Smoke test (closes SVC-128)

1. `curl https://api.eazyserv.com/health` → 200
2. `curl https://api.eazyserv.com/healthz` → 200 (readiness pings DB + Redis)
3. Open Cloudflare Pages URL on phone → sign up with a real number →
   book a real service → verify booking lands in Neon
4. Trigger a test error from Sentry's DSN check page → confirm event
   appears in Sentry within 30s

If all four pass, tag the state:

```bash
# In backend repo
git tag beta-live-$(date +%Y-%m-%d)
git push --tags
```

---

## Part 3 — What Claude runs (per-operation cheat sheet)

Each of these is a one-shot Claude can execute autonomously given the
profile is set up.

### 3.1 Deploy new backend version

```bash
# In backend repo, main branch
docker build -t eazyserv-backend .
docker tag eazyserv-backend:latest \
    $ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/eazyserv-backend:beta
docker push $ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/eazyserv-backend:beta
# App Runner auto-detects new :beta tag, deploys within 2-3 min
```

If migrations are needed:

```bash
DATABASE_URL=$(aws ssm get-parameter --name /eazyserv/beta/DATABASE_URL \
    --with-decryption --query Parameter.Value --output text) \
    uv run alembic upgrade head
# ... THEN push new image
```

### 3.2 Roll back

```bash
# Find previous image digest
aws ecr describe-images --repository-name eazyserv-backend \
    --query 'sort_by(imageDetails,& imagePushedAt)[-2].imageDigest'
# Tag it as :beta
aws ecr batch-get-image --repository-name eazyserv-backend \
    --image-ids imageDigest=<previous-digest> \
    --query 'images[].imageManifest' --output text \
  | aws ecr put-image --repository-name eazyserv-backend \
        --image-tag beta --image-manifest file:///dev/stdin
# App Runner picks up in 2 min
```

### 3.3 Rotate a secret

```bash
aws ssm put-parameter --name /eazyserv/beta/JWT_SECRET_KEY \
    --value "$(openssl rand -base64 32)" --type SecureString --overwrite
# App Runner needs a service restart to pick up SSM changes:
aws apprunner start-deployment --service-arn $SERVICE_ARN
```

### 3.4 Ship a new APK version

Follow `frontend/RELEASE.md` for building + signing the APK. Then:

```bash
aws s3 cp app-release.apk s3://eazyserv-downloads/apk/eazyserv-1.0.1.apk \
    --cache-control 'public, max-age=3600'
aws s3 cp app-release.apk s3://eazyserv-downloads/apk/latest.apk \
    --cache-control 'no-cache'
# Bump the DB row so version-check nudge doesn't fire on the new build
DATABASE_URL=$(aws ssm get-parameter --name /eazyserv/beta/DATABASE_URL \
    --with-decryption --query Parameter.Value --output text) \
    psql "$DATABASE_URL" -c \
    "UPDATE app_versions SET version='1.0.1' WHERE platform='android';"
```

Or use `frontend/scripts/bump_version.mjs` which prints the SQL for you.

### 3.5 Read logs

```bash
# Recent errors
aws logs tail /aws/apprunner/eazyserv-backend/$SERVICE_ID/application \
    --since 15m --filter 'level="error"'

# Live tail (Ctrl-C to stop)
aws logs tail /aws/apprunner/eazyserv-backend/$SERVICE_ID/application \
    --follow
```

### 3.6 Force-block old clients (emergency, SVC-162)

Someone shipped a broken 1.0.1. Everyone on 1.0.1 must upgrade to 1.0.2:

```bash
psql "$DATABASE_URL" -c \
    "UPDATE app_versions SET min_supported_version='1.0.2' WHERE platform='android';"
# In-app version check hits /downloads/latest.json every foreground event.
# 5-min CDN cache is the worst-case reaction time.
```

---

## Part 4 — Environment variable map

Everything the backend reads → where it lives. This is authoritative;
`app/config.py` should match.

| Env var | Source | Notes |
|---|---|---|
| `DATABASE_URL` | SSM `/eazyserv/beta/DATABASE_URL` | Neon pooled URL, `postgresql+asyncpg://...` |
| `SYNC_DATABASE_URL` | SSM `/eazyserv/beta/SYNC_DATABASE_URL` | Same DB, `postgresql+psycopg2://...` for Celery |
| `REDIS_URL` | SSM `/eazyserv/beta/REDIS_URL` | Upstash `rediss://...` |
| `JWT_SECRET_KEY` | SSM | 32-byte random |
| `ENCRYPTION_KEY` | SSM | 32-byte / 44-char base64 (validated at startup, PR #58) |
| `RAZORPAY_KEY_ID` | SSM | Live keys once Razorpay KYC done |
| `RAZORPAY_KEY_SECRET` | SSM | " |
| `RAZORPAY_WEBHOOK_SECRET` | SSM | Set when webhook URL registered on Razorpay side |
| `FCM_CREDENTIALS_PATH` | env `/tmp/fcm.json` | Materialised from base64 SSM at container start (see §5.3) |
| `MSG91_AUTH_KEY` | SSM | Once MSG91 template approved |
| `MSG91_TEMPLATE_ID` | SSM | DLT-approved template ID (`{"var1": otp}` substitution). Empty → send-otp returns 502. |
| `GOOGLE_MAPS_API_KEY` | SSM | For address geocoding |
| `ADMIN_ALLOWED_IPS` | SSM | Vipul's home IP for now; add ops as needed |
| `SENTRY_DSN` | SSM | Backend project DSN |
| `ENVIRONMENT` | env constant | `production` (drives log-level escalation, admin allowlist WARN, etc.) |
| `LOG_JSON` | env constant | `true` (CloudWatch parses JSON logs) |
| `CORS_ALLOWED_ORIGINS` | env constant | `https://eazyserv.com,https://app.eazyserv.com,capacitor://localhost,https://localhost` |

---

## Part 5 — Terraform migration path (do AFTER §2 works end-to-end)

Manual bringup first. Terraform second. In that order — otherwise
we spend a day debugging Terraform state before knowing whether the
architecture is right.

### 5.1 Layout

```
backend/
  terraform/
    beta/
      main.tf              # provider + backend
      variables.tf         # inputs
      ecr.tf               # container registry
      apprunner.tf         # service, auto-scaling config, IAM roles
      s3.tf                # eazyserv-downloads
      ssm.tf               # parameter names (NOT values — those stay manual)
      route53.tf           # OR cloudflare_record.tf if CF-only
      outputs.tf           # service URL, S3 bucket name, etc.
      terraform.tfstate    # local state initially; migrate to S3 backend in §5.4
```

### 5.2 Provider block (`main.tf`)

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region  = "ap-south-1"
  profile = "eazyserv"
}

provider "cloudflare" {
  # api_token via env: CLOUDFLARE_API_TOKEN
}
```

### 5.3 App Runner module (`apprunner.tf`)

Key thing: the `runtime_environment_secrets` block references SSM
parameter ARNs — Terraform doesn't manage the VALUES (those are
sensitive), only the wiring. `ssm.tf` declares the parameter NAMES;
values are set manually via `aws ssm put-parameter`.

```hcl
resource "aws_apprunner_service" "backend" {
  service_name = "eazyserv-backend"

  source_configuration {
    image_repository {
      image_identifier      = "${aws_ecr_repository.backend.repository_url}:beta"
      image_repository_type = "ECR"
      image_configuration {
        port = "8000"
        runtime_environment_variables = {
          ENVIRONMENT            = "production"
          LOG_JSON               = "true"
          # Origins: web app, iOS Capacitor (capacitor://), Android Capacitor (https://localhost — comes from androidScheme: "https" in capacitor.config.ts). Omitting https://localhost blocks every request from Android with an opaque "Failed to fetch" — the browser rejects the CORS preflight and the fetch never fires.
          CORS_ALLOWED_ORIGINS   = "https://eazyserv.com,https://app.eazyserv.com,capacitor://localhost,https://localhost"
        }
        runtime_environment_secrets = {
          DATABASE_URL           = aws_ssm_parameter.database_url.arn
          SYNC_DATABASE_URL      = aws_ssm_parameter.sync_database_url.arn
          REDIS_URL              = aws_ssm_parameter.redis_url.arn
          JWT_SECRET_KEY         = aws_ssm_parameter.jwt_secret.arn
          ENCRYPTION_KEY         = aws_ssm_parameter.encryption_key.arn
          # ... every other secret
        }
      }
    }
    auto_deployments_enabled = true
    authentication_configuration {
      access_role_arn = aws_iam_role.apprunner_ecr_access.arn
    }
  }

  instance_configuration {
    cpu    = "256"   # 0.25 vCPU
    memory = "512"   # 0.5 GB
  }

  health_check_configuration {
    protocol = "HTTP"
    path     = "/health"
    interval = 20
    timeout  = 5
  }
}

# FCM credentials JSON deserves a note: it's a full JSON blob, not
# a scalar. Store base64 in SSM (§2.3); a container startup script
# writes it to /tmp/fcm.json and points FCM_CREDENTIALS_PATH at that.
# Handled in Dockerfile CMD, not in Terraform.
```

### 5.4 State backend (§5 is complete only after this)

Move Terraform state out of local files into S3 with DynamoDB locking,
so Claude and Vipul don't clobber each other:

```bash
aws s3api create-bucket --bucket eazyserv-tfstate \
    --create-bucket-configuration LocationConstraint=ap-south-1
aws s3api put-bucket-versioning --bucket eazyserv-tfstate \
    --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name eazyserv-tfstate-locks \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
```

Then in `main.tf`:

```hcl
terraform {
  backend "s3" {
    bucket         = "eazyserv-tfstate"
    key            = "beta/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "eazyserv-tfstate-locks"
    encrypt        = true
  }
}
```

`terraform init -migrate-state` moves the local state up.

### 5.5 CI wiring (SVC-109 closer)

`.github/workflows/deploy.yml`:

```yaml
name: Deploy backend to beta
on:
  push:
    branches: [main]
    paths: ['backend/**']
concurrency: deploy-beta
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write   # OIDC to AWS
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::${{ secrets.AWS_ACCOUNT_ID }}:role/eazyserv-github-deploy
          aws-region: ap-south-1
      - uses: aws-actions/amazon-ecr-login@v2
      - run: docker build -t eazyserv-backend backend/
      - run: |
          docker tag eazyserv-backend:latest \
              $ECR_REGISTRY/eazyserv-backend:beta
          docker push $ECR_REGISTRY/eazyserv-backend:beta
        env:
          ECR_REGISTRY: ${{ steps.login-ecr.outputs.registry }}
      # Alembic runs against Neon; not gated on App Runner
      - run: uv run alembic upgrade head
        env:
          DATABASE_URL: ${{ secrets.NEON_DATABASE_URL }}
```

GitHub → OIDC to AWS avoids storing access keys as secrets. IAM setup
is one `aws_iam_openid_connect_provider` + one `aws_iam_role` with a
trust policy — happy to write it when we get there.

---

## Part 6 — Cost projection

At beta scale (1000 users, 500 bookings/day, 5000 push/day):

| Item | Free tier | Beta usage | Bill |
|---|---|---|---|
| App Runner (0.25 vCPU) | none | ~$5/mo | $5.00 |
| ECR storage (< 500 MB) | 500 MB/mo forever free | ~200 MB | $0.00 |
| S3 (APK + logs) | 5 GB / 20k GET / 2k PUT | trivial | ~$0.10 |
| SSM SecureString | 10k params + std throughput free | 15 params | $0.00 |
| CloudWatch logs (5 GB/mo free) | 5 GB free | ~2 GB | $0.00 |
| Data transfer out (100 GB free) | 100 GB free | ~5 GB | $0.00 |
| Route 53 (if used) | none | 1 hosted zone | $0.50 (skip — use Cloudflare) |
| Neon Postgres | 3 GB storage, 100 compute hrs | fits | $0.00 |
| Upstash Redis | 10k commands/day, 256 MB | fits | $0.00 |
| Sentry Developer | 5k errors/mo | fits | $0.00 |
| **Total** | | | **~$5–8/mo** |

Post-beta growth: RDS + ElastiCache once we outgrow free tiers.
Migration is a `pg_dump` → RDS restore + Redis DB flush → connection
string swap. Small, planned, one-hour operation.

---

## Part 7 — Post-bootstrap hardening (do after beta launch)

Not for now. Filed here so we don't lose them:

1. **Narrow IAM policy** — replace `AdministratorAccess` on the
   `eazyserv-claude-beta` user with the specific actions Claude
   actually needs (RunTask on App Runner, PutParameter on
   `/eazyserv/beta/*`, S3 put/get on eazyserv-downloads/*, ECR push,
   CloudWatch read). Recipe: audit CloudTrail for 2 weeks of Claude
   actions → generate least-privilege policy.
2. **Rotate everything** — the JWT / encryption keys generated during
   bootstrap should be rotated once real users exist, on a scheduled
   cadence (see `SECRETS.md`).
3. **RDS migration** if we hit Neon's free-tier ceiling (3 GB / 100 CH).
4. **CloudWatch alarms** on: 5xx rate > 1%, p95 latency > 500ms,
   memory > 80%.
5. **PostGIS backup verification** — Neon does daily automated backups
   but we should verify a restore quarterly (per `BACKUPS.md`).
6. **Structured deploy notifications** — Slack webhook wired to
   CloudWatch Events on App Runner deploy success/failure.

---

## Appendix — troubleshooting quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| App Runner "unhealthy" | `/health` unreachable | Check Dockerfile CMD, port 8000 exposed |
| App Runner deploy fails "no such image" | Wrong tag in ECR | `docker push ... :beta` |
| 500s on all endpoints post-deploy | Env var missing | Check App Runner env config; every SSM ref must exist |
| CORS errors in browser | Origin not in `CORS_ALLOWED_ORIGINS` | Update env var, redeploy |
| Alembic "role sc_user does not exist" | Wrong DATABASE_URL | Neon uses its own username; check the string |
| Push not delivering | FCM_CREDENTIALS_PATH not materialised | Container start script must write JSON from base64 SSM |
| SSM param decrypt fails from App Runner | Service role missing `kms:Decrypt` on aws/ssm key | Add to `apprunner_instance_role` policy |
