# TODO: capture real MSG91 /api/v5/otp response bodies (SVC-460)

Capture both:

- `success__*.json` — a successful send: `{"type":"success", "request_id":"..."}`
- `error-silent__*.json` — the silent-failure body MSG91 returns with HTTP
  200: `{"type":"error","message":"..."}`

Both exist in beta CloudWatch; the MSG91 dashboard is also a source.

Redaction: strip `mobile` / `userMobile` / any phone numbers. Keep `type`,
`message`, `request_id`.

Once both are present, the SVC-460 handler change (SMSDeliveryFailed on
non-success `type`) can land safely with these as the canary payloads.
