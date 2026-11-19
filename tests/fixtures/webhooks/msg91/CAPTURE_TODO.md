# MSG91 /api/v5/otp response corpus — status (SVC-460)

## Current state (docs-sourced)

2 payloads landed from the MSG91 public docs + community reports of
the exact shape MSG91 returns on error:

- `success__docs_otp_sent.json` — `{"type":"success", ...}`
- `error__docs_rejected.json` — `{"type":"error", "message":"..."}` (the
  silent-failure body MSG91 returns with HTTP 200)

The `__docs_<id>` suffix marks these as docs-sourced. Replace with
`__captured_<id>` when real traffic is pulled from beta CloudWatch.

## Why both payloads matter

`reference_msg91_otp_gotchas.md` calls out two silent-fail modes.
The error fixture here captures the shape the gotcha's "HTTP 200 +
`type: error`" trap sends — any refactor that keys off HTTP status
alone fails the schema check because `type: "error"` + a nullable
`request_id` doesn't match our success contract.

## Still TODO

- Replace with real `__captured_*` from beta CloudWatch to catch
  MSG91-specific drift the docs don't publicise. The MSG91 dashboard
  also exposes recent request/response bodies.

Redaction: strip `mobile` / `userMobile` / any phone numbers. Keep
`type`, `message`, `request_id`.

Once both are production-captured, the SVC-460 handler change
(SMSDeliveryFailed on non-success `type`) can land safely with these
as the canary payloads.
