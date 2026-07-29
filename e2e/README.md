# E2E Smoke Suite (SC-52)

Nightly Playwright smoke against staging. Kept in its own npm project so the
backend deps stay Python-only and Playwright's `node_modules` doesn't bloat
the Fargate image.

## What it covers

- `auth-smoke.spec.ts` — send-OTP → verify-OTP → token issued.
- Health probes (`/health`, `/healthz`) as sanity checks.

Runs against staging via the E2E OTP bypass (`E2E_TEST_PHONES` + `E2E_TEST_OTP`
on the backend) so no SMS provider is in the loop. See `app/services/auth_service.py`
for the guard logic — production is a hard-refuse.

## Running locally

```bash
cd e2e
npm install
STAGING_API_URL=https://api.staging.serviceconnect.in \
E2E_TEST_PHONE=+919999900001 \
E2E_TEST_OTP=123456 \
  npm test
```

Point at your own dev backend by swapping `STAGING_API_URL` for `http://localhost:8000`
and setting `E2E_TEST_PHONES` + `E2E_TEST_OTP` in `../.env` accordingly.

## CI schedule

`.github/workflows/e2e-nightly.yml` runs at 20:30 UTC (02:00 IST). Failures
email the repo owner + last committer. Report HTML is uploaded as a
GH Actions artifact for 14 days.

## Extending

- **More API-level tests** — add `*.spec.ts` under `tests/`. The config uses
  Playwright's `request` fixture, no browser boot.
- **Real browser flow** — flip on chromium via a new project block in
  `playwright.config.ts` and `use.browserName = "chromium"`. Currently the
  workflow installs chromium (line 33 of the workflow) in anticipation.

## Why Playwright and not pytest+httpx

Ticket mandated Playwright. HTTP-only smoke would have been simpler as
pytest + httpx (reusing the Python toolchain). Playwright is nonetheless
useful groundwork: when we add browser tests for the frontend later, the
scaffold and CI wiring don't need to be redone.
