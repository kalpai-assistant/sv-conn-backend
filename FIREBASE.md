# Firebase / FCM Runbook

Companion to [SECRETS.md](SECRETS.md), [INFRA.md](INFRA.md),
[OBSERVABILITY.md](OBSERVABILITY.md).

**TL;DR** — We use Firebase for **one thing: FCM push notifications**. Nothing
else. Push targets are Android APK (Capacitor) + iOS Safari PWA (Web Push) +
Chrome Android PWA. Everything runs on Firebase's free Spark plan — no billing.

---

## What Firebase gives us (and what it doesn't)

| Product | Use | Free tier |
|---|---|---|
| **Cloud Messaging (FCM)** | ✅ Push to Android APK + PWA | No usage limits, ever |
| Firebase Auth | ❌ We use MSG91 OTP | 50k MAU |
| Firestore | ❌ We use Postgres | 1 GiB |
| Cloud Functions | ❌ We use FastAPI + Celery | 2M invocations/mo |
| Cloud Storage | ❌ We use S3 | 5 GB |
| Hosting | ❌ We use Cloudflare | 10 GB + 360 MB/day |
| Analytics / Crashlytics | ❌ Sentry covers | Free |

**Stay on Spark (free) plan.** Nothing we use requires Blaze. If Blaze
gets enabled accidentally, disable immediately — a runaway export/query
against BigQuery is the only real cost risk on our surface.

---

## Bundle IDs — pin these before creating anything

The bundle ID pins the "identity" of the app across Firebase, Google Play (post-beta),
and Android's install system. **Cannot be changed after publish.** Locked in as:

- **Android app** — `com.eazyserv.app`
- **iOS** — SKIP for beta (no native iOS; PWA only)
- **Web** — no bundle ID, just a display name (e.g. `Eazyserv Web`)

---

## First-time project setup (CLI walkthrough)

Run everything from a terminal on a machine where you've done `firebase login`
and `gcloud auth login`. Both need to be authenticated as the **owner** of the
GCP project.

### 1. Prerequisites

```bash
# Install CLIs (one-time per machine)
npm install -g firebase-tools
brew install --cask google-cloud-sdk   # Mac. Linux: apt install google-cloud-cli

# Sign in — both, browser opens
firebase login
gcloud auth login

# Also set Application Default Credentials — some management API calls need this
gcloud auth application-default login
gcloud auth application-default set-quota-project <PROJECT_ID>
```

**Watch for:** if your Firebase CLI gives 403 later on `projects:addfirebase`,
run `firebase login --reauth` — its cached OAuth scopes may not include the
Firebase Management API scope. `--reauth` forces a fresh consent screen.

### 2. Add Firebase to an existing GCP project

If you created a plain GCP project (via `gcloud projects create` or the console),
Firebase isn't automatically enabled on it. Do this once:

```bash
export PROJECT_ID="eazyserv-2024"   # your GCP project ID

# Enable the Firebase Management API on the project
gcloud services enable firebase.googleapis.com \
    fcm.googleapis.com \
    cloudmessaging.googleapis.com \
    --project="$PROJECT_ID"

# Wire the GCP project as a Firebase project
firebase projects:addfirebase "$PROJECT_ID"

# Point CLIs at it
gcloud config set project "$PROJECT_ID"
firebase use "$PROJECT_ID"
```

**Fallback (console) if the CLI fails:**
- Open https://console.firebase.google.com → **Add project** → pick "Add
  Firebase to Google Cloud project" → select `eazyserv-2024`
- Skip Analytics (we use Sentry)

### 3. Register the Android app

```bash
firebase apps:create ANDROID \
    --package-name com.eazyserv.app \
    --display-name "Eazyserv Android" \
    --project "$PROJECT_ID"
# → the output lists the "App ID", format:  1:1234567890:android:abcdef
export ANDROID_APP_ID="1:X:android:Y"

# Download google-services.json (Android needs this at build time)
firebase apps:sdkconfig ANDROID "$ANDROID_APP_ID" \
    --out frontend/android/app/google-services.json
```

### 4. Register the Web app (for PWA push)

```bash
firebase apps:create WEB "Eazyserv Web" --project "$PROJECT_ID"
export WEB_APP_ID="1:X:web:Y"

# Download the web SDK config (JSON, embedded into PWA client)
firebase apps:sdkconfig WEB "$WEB_APP_ID" \
    --out frontend/src/firebase-config.json
```

### 5. Generate the VAPID key pair (Web Push) — **console step**

The one thing `firebase-tools` doesn't yet expose. ~30 seconds:

1. Firebase Console → project → ⚙ **Project settings** → **Cloud Messaging** tab
2. Scroll to **Web configuration** → **Web Push certificates**
3. Click **Generate key pair**
4. Copy the "Key pair" value (long base64url string, starts with `B…`, ~87 chars)

This is the **VAPID public key** — safe to commit to the frontend; it's
delivered to the browser as part of the subscription flow.

### 6. Create a service account for the backend

The backend sends pushes via `firebase-admin` SDK, which authenticates using
a service account JSON key.

```bash
# Create the service account
gcloud iam service-accounts create eazyserv-fcm-sender \
    --display-name="Eazyserv FCM Sender" \
    --project="$PROJECT_ID"

# Grant only the "send FCM messages" role — nothing else
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:eazyserv-fcm-sender@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/firebasemessaging.admin"

# Download the key JSON
gcloud iam service-accounts keys create ~/eazyserv-fcm-sa.json \
    --iam-account="eazyserv-fcm-sender@${PROJECT_ID}.iam.gserviceaccount.com"
```

### 7. Store the secret + delete the local copy

The service account JSON is a **real secret** (see [SECRETS.md](SECRETS.md)):

- **1Password** ("Eazyserv ops" vault): store the JSON file + record the
  associated project ID.
- **Production**: base64-encode and push to SSM at
  `/sc/prod/FCM_CREDENTIALS_JSON` (see INFRA.md §9). The Fargate task def
  reads it into `FCM_CREDENTIALS_JSON` env var; app code base64-decodes to
  a temp file and points `FCM_CREDENTIALS_PATH` at it.
- **Staging**: same pattern under `/sc/staging/FCM_CREDENTIALS_JSON`.
- **Local dev**: keep the file at `~/eazyserv-fcm-sa.json`, set
  `FCM_CREDENTIALS_PATH=~/eazyserv-fcm-sa.json` in your `.env`.

After you've stored the JSON in 1Password + SSM, shred the copy on disk:

```bash
shred -u ~/eazyserv-fcm-sa.json
```

---

## Where things end up in the repos

**Frontend**:
| File | Committed? | Notes |
|---|---|---|
| `frontend/android/app/google-services.json` | ❌ Gitignored | Injected in CI from a base64-encoded GitHub Secret. Local dev keeps a copy per developer. |
| `frontend/src/firebase-config.json` | ❌ Gitignored | Same as above. |
| VAPID public key | ✅ Committed | Not a secret — the public half of a key pair; safe to include in the client bundle. |

**Backend**:
| Setting | Where |
|---|---|
| `FCM_CREDENTIALS_PATH` | Env var pointing at a file on disk |
| `FCM_CREDENTIALS_JSON` | SSM SecureString (prod/staging), base64 |

---

## Rotation

### Service account key

**Cadence:** annually, or on personnel change / compromise (per SECRETS.md).

```bash
# Generate a fresh key
gcloud iam service-accounts keys create ~/eazyserv-fcm-sa-new.json \
    --iam-account="eazyserv-fcm-sender@${PROJECT_ID}.iam.gserviceaccount.com"

# Push to SSM (prod shown)
base64 -i ~/eazyserv-fcm-sa-new.json | aws ssm put-parameter \
    --name /sc/prod/FCM_CREDENTIALS_JSON \
    --value file:///dev/stdin \
    --type SecureString --overwrite --region ap-south-1

# Force-deploy Fargate (workers + api)
aws ecs update-service --cluster sc-prod --service sc-api \
    --force-new-deployment --region ap-south-1
aws ecs update-service --cluster sc-prod --service sc-workers \
    --force-new-deployment --region ap-south-1

# Confirm a push works via an actual send (test booking or admin trigger)

# LIST existing keys — pick the OLD key ID:
gcloud iam service-accounts keys list \
    --iam-account="eazyserv-fcm-sender@${PROJECT_ID}.iam.gserviceaccount.com"

# DELETE the old key
gcloud iam service-accounts keys delete <OLD_KEY_ID> \
    --iam-account="eazyserv-fcm-sender@${PROJECT_ID}.iam.gserviceaccount.com"

# Shred the local copy
shred -u ~/eazyserv-fcm-sa-new.json
```

Do NOT delete the old key until the new key has been confirmed working with a
real push — otherwise a botched rotation kills the notification pipeline.

### VAPID key pair

**Cadence:** never routinely. Rotate only on compromise.

Rotating VAPID **invalidates every existing PWA push subscription** — every
iOS user must re-open the app in Safari for a fresh subscription. There is
no "graceful" rotation; this is a wipe. Only do it if the key is genuinely
compromised.

### google-services.json / firebase-config.json

Not secrets. Regenerate at any time via `firebase apps:sdkconfig` — they're
deterministic outputs of the project settings, so unless the project was
reconfigured, the regenerated file is byte-identical to what's already in
CI.

---

## Verification

### FCM initialisation

```bash
uv run python -c "
from firebase_admin import credentials, initialize_app
cred = credentials.Certificate('/path/to/eazyserv-fcm-sa.json')
app = initialize_app(cred)
print('FCM initialised:', app.project_id)
"
```

Prints `FCM initialised: eazyserv-2024` on success. If the credentials are
wrong or the file is missing, it raises loudly.

### Send a test push

```bash
uv run python -c "
import firebase_admin
from firebase_admin import credentials, messaging
firebase_admin.initialize_app(credentials.Certificate('/path/to/eazyserv-fcm-sa.json'))

# Replace with a real device token (obtain from the app's registration listener)
token = 'REPLACE_ME'
resp = messaging.send(messaging.Message(
    notification=messaging.Notification(title='Test', body='Hello from CLI'),
    token=token,
))
print('sent:', resp)
"
```

Prints `sent: projects/.../messages/...` on success. The device receives a
notification (Android) or the browser fires the `push` service worker event
(PWA).

---

## What this runbook deliberately does NOT cover

- **APNs (Apple Push Notification Service) `.p8` upload** — needed only for
  native iOS push. We're PWA-only for iOS beta; no APNs.
- **Analytics** — Sentry covers errors + our own logs cover product metrics.
- **BigQuery export** — requires Blaze plan, not needed.
- **Multi-project setup** (staging vs prod on separate Firebase projects) —
  worth doing post-beta if we ever have staging traffic hitting real devices.
  For beta, one project is enough.
- **Team access management** — grant `roles/firebase.admin` to any additional
  humans who need to manage the project. Do NOT share the service account key
  for human use.
