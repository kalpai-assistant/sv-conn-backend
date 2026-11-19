# Webhook contract test fixtures (SVC-458 / 459 / 460)

This directory holds captured real webhook payloads + JSON-Schema files for
each external vendor's webhook. Pytest in `tests/test_webhook_contracts.py`
asserts every captured payload validates against its schema.

## Why "captured real payloads" and not synthetic ones

The whole point of a contract test is to catch the day the vendor renames
a field. Hand-written fake payloads just test that our Pydantic model
matches our Pydantic model — zero signal. Only payloads that actually hit
production tell us what the vendor is actually sending.

## How to add a new captured payload

1. From CloudWatch, pull a recent webhook body hitting one of:
   - `/payments/webhook` — Razorpay (SVC-458)
   - `/payments/payout-webhook` — RazorpayX (SVC-459; **blocked on SVC-229**)
   - MSG91 response body logged by `app/utils/sms_client.py::MSG91Client.send_otp` (SVC-460)

2. Redact PII:
   - Razorpay: `notes`, `email`, `contact`, `fingerprint`, any `description`.
   - RazorpayX: `fund_account.account_number`, `fund_account.ifsc`, `notes`.
   - MSG91: `mobile`, `userMobile`, any phone numbers.

3. Save as `<vendor>/<event-kind>__<short-id>.json`. Example:
   - `razorpay/payment-captured__pay_P0xy5t.json`
   - `razorpayx/payout-processed__pout_Pz2x9q.json`

4. Run `uv run pytest tests/test_webhook_contracts.py -v`. If the new payload
   fails validation, update `<vendor>/schema.json` to pin the real shape —
   do NOT loosen the schema just to make it pass.

## Schemas pin what WE CONSUME, not the whole vendor surface

Each `<vendor>/schema.json` is deliberately narrower than the vendor's full
API. We only require the fields `app/services/*.py::handle_webhook` and
friends actually read. A vendor adding new fields outside that subset must
not flake the test; a vendor renaming / dropping a field we read must.
