# RazorpayX activation checklist

## TL;DR

RazorpayX is **off by default** (`RAZORPAYX_ENABLED=False`). All five
integration surfaces refuse / log-and-ignore while off:

1. `razorpayx_client.create_payout` → `RazorpayXDisabledError` (never hits the wire)
2. `razorpayx_client.list_payouts` → `RazorpayXDisabledError`
3. `PayoutService.preflight_payout` → `PayoutRefused` (admin route 402)
4. `PayoutService.create_provider_payout` → `PayoutRefused`, **no audit row written**
5. `PayoutService.handle_payout_webhook` → silent accept + log (no state change)
6. `PayoutService.reconcile_payouts` → noop, returns `{skipped_reason: ...}`
7. `/payments/payout-webhook` route → 200 OK + log, skips signature verification

**When this flag is off, no live RazorpayX call can fire and no vendor
webhook can touch our state — regardless of how the SSM creds are set.**
A dev with prod `.env` on their laptop cannot accidentally pay a provider.

## Pre-activation checklist

Before setting `RAZORPAYX_ENABLED=True` in App Runner:

- [ ] **RazorpayX account is live** (SVC-229). Account approved, funding
      virtual account provisioned, dashboard accessible.
- [ ] **SSM parameters populated** in `ap-south-1` for the target environment:
  - `/eazyserv/beta/RAZORPAYX_ACCOUNT_NUMBER` → the funding account
  - `/eazyserv/beta/RAZORPAYX_WEBHOOK_SECRET` → shared secret for
    `/payments/payout-webhook` signature verification
  - `/eazyserv/beta/RAZORPAY_KEY_ID` + `/eazyserv/beta/RAZORPAY_KEY_SECRET`
    already present (shared between Payments + RazorpayX)
- [ ] **Beta canary**: run a ₹1 payout to an internal test SP via the
      admin console. Verify audit row, webhook callback, and
      `settlement_payout_id` populated on the booking.
- [ ] **Reconcile smoke**: trigger `reconcile_payouts` manually once
      (Celery beat runs it nightly otherwise). Report should show
      `checked: 1, case_a: 0, case_b: 0, unresolved: 0`.
- [ ] **Capture the first real webhook payload** and replace
      `tests/fixtures/webhooks/razorpayx/payout-processed__docs_*.json`
      with `__captured_*` — the docs-sourced placeholders ship with the
      kill switch; captured payloads harden the contract test against
      production reality.
- [ ] **Monitor** CloudWatch for `payout.reconcile.skipped` / `payout.webhook.ignored`
      log lines in the 48h after flipping — zero lines expected once the flag is on.

## Flipping the flag

1. Update App Runner service env var: `RAZORPAYX_ENABLED=true`.
2. App Runner redeploys (~3-5 min).
3. The next admin-triggered payout clears `RAZORPAYX_ENABLED` at the
   `preflight_payout` gate and proceeds through `create_provider_payout`.
4. The first webhook callback hits the signature-verified path and lands
   in `handle_payout_webhook`.

## Flipping it back off (incident response)

If a RazorpayX integration issue appears in production:

1. Flip `RAZORPAYX_ENABLED=false` in App Runner env (fast — one env var).
2. App Runner redeploys.
3. **Payouts in flight** at the moment of the flip: any `attempting` audit
   row written before the flip may still receive its RazorpayX response.
   Confirm manually in the RazorpayX dashboard before any retry; the
   refuse-on-open-attempt gate in `_check_payable` prevents a naive retry
   from paying twice.
4. **Inbound webhooks** during the disabled window are accepted + logged
   but ignored. Once the integration is re-enabled, run
   `reconcile_payouts` manually with a wider look-back
   (`PAYOUT_RECONCILE_WINDOW_SECONDS` = the gap duration) to catch any
   drift from webhooks that were ignored.

## Testing the kill switch

`tests/test_razorpayx_kill_switch.py` covers every one of the seven gates.
Run locally:

```bash
uv run pytest tests/test_razorpayx_kill_switch.py -v
```

The autouse conftest fixture flips `RAZORPAYX_ENABLED=True` for the whole
suite (because the existing 3,500 LOC of payout tests exercise the service
layer with a mocked vendor). Each kill-switch test re-flips it False
locally via `monkeypatch.setattr` to verify the gate.
