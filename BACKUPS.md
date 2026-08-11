# Database Backups & Restore Runbook (SC-34)

Operational procedure for the Eazyserv production database. Applies once
the AWS RDS PostgreSQL instance is provisioned (SC-44). Written to be
followed by anyone with AWS console access — no institutional knowledge
required.

**TL;DR** — Enable automated backups with 14-day retention, PITR on,
encryption on, cross-region snapshot copy monthly. Perform a full restore
drill every quarter (monthly during beta) and log the outcome. If prod is on
fire, jump to [Emergency restore](#emergency-restore).

---

## What we're backing up

- **RDS PostgreSQL** with the PostGIS extension. Instance class TBD by SC-44
  (recommended `db.t4g.small` at Beta, `db.r6g.large` at Growth).
- **Tables of record:** users, bookings, payments, provider profiles, KYC
  metadata, reviews.
- **Not backed up here:** Redis (regenerable / TTL data), S3 objects (S3 has
  its own versioning + lifecycle — separate runbook).
- **PII surface:** Aadhaar/PAN encrypted at column level (see
  `app/utils/encryption.py`); KYC images live in S3, not the DB. Restoring
  the DB does NOT restore the KYC images — that's a separate S3 concern.

---

## Automated backups (configure once, verify every quarter)

### Settings — required at instance creation or via `modify-db-instance`

| Setting | Value | Rationale |
|---|---|---|
| `BackupRetentionPeriod` | **14 days** | 2× RDS default (7). Long enough to recover from a bad Sunday-morning migration discovered on the following Friday. |
| `PreferredBackupWindow` | **21:30–22:30 UTC** | 03:00–04:00 IST — off-peak for Indian traffic. |
| `PreferredMaintenanceWindow` | Sunday 22:30–23:30 UTC | 04:00–05:00 IST Sunday. Distinct from backup window. |
| `StorageEncrypted` | `true` | KMS default key. Required for KYC data (DPDP). Cannot be changed later — must be set at creation. |
| `DeletionProtection` | `true` | Prevents accidental instance deletion. Toggle off only when consciously destroying. |
| `EnabledCloudwatchLogsExports` | `["postgresql"]` | Query logs in CloudWatch for forensic use. |

### Verify daily backup is running (CloudWatch)

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name SnapshotStorageUsed \
  --dimensions Name=DBInstanceIdentifier,Value=<your-instance-id> \
  --start-time $(date -u -v-2d +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 --statistics Maximum
```

Snapshot storage should be increasing daily by roughly one DB size. If it's
flat for 48+ hours, automated backups are broken — investigate immediately.

### CloudWatch alarm (highly recommended)

```
Metric:    RDS/SnapshotStorageUsed
Statistic: Maximum, 24h period
Condition: LessThanOrEqualToThreshold, threshold = <yesterday's value>
Action:    Slack #ops-alerts + on-call PagerDuty
```

---

## Point-in-time recovery (PITR)

Enabled automatically when `BackupRetentionPeriod > 0`. Recovers to any
second within the retention window. Uses transaction logs shipped to S3
every 5 minutes (RDS-managed, no config).

**What this covers well:** accidental `DELETE`/`UPDATE`, bad migration,
truncated table.
**What it does NOT cover:** logical corruption caught weeks later (falls
outside 14-day window), regional AWS outage (see cross-region section).

---

## Cross-region snapshot copies (monthly, manual for now)

RDS automated backups live in the same region as the DB. If `ap-south-1`
Mumbai goes down for a day (has happened), automated backups are also
unavailable.

**Beta scope:** monthly manual snapshot + copy to `ap-southeast-1` Singapore.
Retain 3 months. Automation post-beta via EventBridge → Lambda.

```bash
# 1. Take a manual snapshot in Mumbai
aws rds create-db-snapshot \
  --db-instance-identifier eazyserv-prod \
  --db-snapshot-identifier eazyserv-monthly-$(date +%Y-%m-%d)

# 2. Wait until it's available (check state = "available")
aws rds describe-db-snapshots \
  --db-snapshot-identifier eazyserv-monthly-$(date +%Y-%m-%d)

# 3. Copy to Singapore
aws rds copy-db-snapshot \
  --source-db-snapshot-identifier arn:aws:rds:ap-south-1:<account>:snapshot:eazyserv-monthly-$(date +%Y-%m-%d) \
  --target-db-snapshot-identifier eazyserv-monthly-$(date +%Y-%m-%d) \
  --source-region ap-south-1 \
  --region ap-southeast-1 \
  --kms-key-id alias/aws/rds
```

Delete snapshots older than 90 days by hand until we automate.

---

## Restore drill (quarterly minimum, monthly during beta)

**Goal:** prove that the backup we think exists can actually restore into a
working database. A backup that hasn't been restore-tested is not a backup.

**Duration:** ~30 minutes end-to-end.
**Cost:** ~₹100 for the ~1h the restored instance runs.

### Procedure

```bash
# Configuration
SOURCE_INSTANCE=eazyserv-prod
DRILL_INSTANCE=eazyserv-restore-drill-$(date +%Y-%m-%d)
```

**1. Pick a snapshot** (either the most recent automated backup or a
specific PITR timestamp).

```bash
aws rds describe-db-snapshots \
  --db-instance-identifier $SOURCE_INSTANCE \
  --snapshot-type automated \
  --query 'DBSnapshots | sort_by(@,&SnapshotCreateTime) | [-1].DBSnapshotIdentifier'
```

**2. Restore into a NEW instance.** Never restore over a live prod
instance — that's what "delete + recreate" would look like from a user
perspective.

```bash
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier $DRILL_INSTANCE \
  --db-snapshot-identifier <snapshot-id-from-step-1> \
  --db-instance-class db.t4g.small \
  --no-publicly-accessible \
  --db-subnet-group-name eazyserv-private-subnets \
  --vpc-security-group-ids <sg-id>
```

**3. Wait for `available`** (~10–15 minutes for a small DB).

```bash
aws rds wait db-instance-available --db-instance-identifier $DRILL_INSTANCE
```

**4. Verify the restore**. Connect via bastion or session-manager tunnel and
run these queries:

```sql
-- 4a. PostGIS extension is active
SELECT postgis_version();

-- 4b. All expected tables exist
\dt
-- Expected: users, bookings, payments, provider_profiles, provider_services,
-- services, categories, reviews, addresses, booking_status_logs, alembic_version

-- 4c. Alembic migration head matches production
SELECT version_num FROM alembic_version;

-- 4d. Row counts within an expected range (no truncation)
SELECT
  'users' AS tbl, COUNT(*) FROM users UNION ALL
SELECT 'bookings', COUNT(*) FROM bookings UNION ALL
SELECT 'payments', COUNT(*) FROM payments UNION ALL
SELECT 'provider_profiles', COUNT(*) FROM provider_profiles;

-- 4e. Recent activity present (transaction logs applied for PITR)
SELECT MAX(created_at) FROM bookings;
-- Should be within ~5min of the target restore timestamp

-- 4f. Encrypted PII decrypts correctly (sanity — pick one known provider)
-- Run app-side, not SQL: verify that fetching this user's profile via
-- /providers/me returns readable values. If decrypt() fails, the KMS key
-- for the encryption env var didn't survive the restore — investigate.
```

**5. Log the drill** in `docs/drills.log` (or a shared doc):

```
Date:        <yyyy-mm-dd>
Snapshot:    <id + timestamp>
Restored to: <drill instance id>
Time to available: <minutes>
Verifications: [pass/fail for each of 4a-4f]
Notes:       <anything unexpected>
```

**6. Tear down** to stop the meter.

```bash
aws rds delete-db-instance \
  --db-instance-identifier $DRILL_INSTANCE \
  --skip-final-snapshot \
  --delete-automated-backups
```

### Drill failure means

Any failing verification (missing table, wrong migration head, decrypt
failure, empty row counts) — the backup is not usable as-is. Do not delete
the drill instance until root cause is understood and documented.

---

## Emergency restore (prod is down / corrupted)

Rehearse this. Do NOT do it for the first time under pressure.

### Step 1 — decide the restore point

- **Corruption discovered within seconds/minutes:** PITR to the moment
  before it happened. Requires knowing the exact timestamp; check
  application logs / Sentry for the first bad request.
- **Corruption discovered hours/days later:** restore from the automated
  snapshot immediately preceding the incident.

### Step 2 — restore to a NEW instance

Same command as [step 2 of the drill](#procedure) but into a name like
`eazyserv-restore-<incident-id>`. Never restore in place.

### Step 3 — verify (steps 4a–4f above), then cutover

1. Put the application into read-only / maintenance mode (backend
   `MAINTENANCE=1` env var — TODO in SC-44).
2. Update the app's `DATABASE_URL` to point at the restored instance.
3. Roll the app pods to pick up the new URL.
4. Run the same verification queries against the app's endpoints
   (`GET /bookings/<known-good-id>`, etc.).
5. Take the app out of maintenance mode.

### Step 4 — rename (optional but recommended)

After stabilising, rename the restored instance to the canonical prod name
so future ops muscle memory works:

```bash
aws rds modify-db-instance \
  --db-instance-identifier eazyserv-restore-<incident-id> \
  --new-db-instance-identifier eazyserv-prod \
  --apply-immediately
```

The old (corrupted) instance is renamed first with a `-postincident` suffix
and kept for 7 days as a forensic artefact.

### Step 5 — post-mortem

Within 48 hours: what caused it, why the drill didn't catch it, what
runbook/monitoring change would have caught it earlier. Update this doc.

---

## Schedule (calendar it)

| Cadence | Task | Owner |
|---|---|---|
| Daily (automated) | RDS automated backup + PITR log ship | AWS |
| Daily (monitored) | Snapshot storage growth alarm | On-call |
| Monthly | Manual snapshot + cross-region copy to `ap-southeast-1` | Ops |
| Monthly (during beta) | Full restore drill | Ops (Lead reviews log) |
| Quarterly (post-beta) | Full restore drill | Ops |
| Quarterly | Delete cross-region snapshots > 90 days | Ops |
| Annually | Review this runbook — instance class, region choice, retention window | Lead + Ops |

---

## Open items (unblock in SC-44)

- [ ] Provision the RDS instance itself (SC-44)
- [ ] Set the CloudWatch alarm on snapshot growth
- [ ] Automate the monthly cross-region copy via EventBridge + Lambda
- [ ] Add the `MAINTENANCE=1` env-var gate to the API for planned
      maintenance windows
- [ ] Cross-region snapshot restore is untested — do it in the first
      quarterly drill after cutting over to real Mumbai + Singapore snapshots

Until SC-44 lands, this runbook lives here as intent — nothing is yet
provisioned to back up.
