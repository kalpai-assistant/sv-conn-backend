# Razorpay webhook corpus — status (SVC-458)

## Current state (docs-sourced)

11 payloads landed from the Razorpay public docs, covering every event
kind our handler branches on:

- `payment-captured__*.json`
- `payment-failed__*.json`
- `order-paid__*.json`
- `refund-created__*.json`
- `refund-processed__*.json`
- `refund-failed__*.json`
- `payment-dispute-created__*.json`
- `payment-dispute-won__*.json`
- `payment-dispute-lost__*.json`
- `payment-dispute-closed__*.json`
- `payment-dispute-under-review__*.json`

The filename suffix `__docs_<id>` is the convention that marks a payload
as **docs-sourced**, not production-captured. A payload captured from
CloudWatch later should use `__captured_<id>` so a reviewer can tell
which is which at a glance.

## Still TODO

- Replace `__docs_*` payloads with real `__captured_*` as beta traffic
  accumulates. The schema lives + doesn't change — only the fixtures swap.
  `settlement-processed` was recovered from a cached Razorpay example after
  the primary docs page went 404 (2026-11-19); still worth capturing a real
  one once settlements fire on beta, in case the US-region variant diverges.

## Why docs-sourced is OK as an interim

The schema (`schema.json`) pins the shape we read; the fixtures prove
the vendor's own canonical example matches that shape. A vendor drift in
the fields we consume still fails this test. What docs can't catch that
captured can: a production payload that silently diverges from the docs
(Razorpay has done this with `contact` / `customer_id` / UPI collect
fields in the past). That's why `__captured_*` remains the goal.

See `../README.md` for the capture + redaction procedure.
