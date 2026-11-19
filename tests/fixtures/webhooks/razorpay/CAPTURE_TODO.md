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

- `settlement-processed__*.json` — Razorpay's docs page for this event is
  404 at the time of writing. Capture from beta CloudWatch when the first
  settlement lands, or sub in a docs example if Razorpay restores the page.
- Replace `__docs_*` payloads with real `__captured_*` as beta traffic
  accumulates. The schema lives + doesn't change — only the fixtures swap.

## Why docs-sourced is OK as an interim

The schema (`schema.json`) pins the shape we read; the fixtures prove
the vendor's own canonical example matches that shape. A vendor drift in
the fields we consume still fails this test. What docs can't catch that
captured can: a production payload that silently diverges from the docs
(Razorpay has done this with `contact` / `customer_id` / UPI collect
fields in the past). That's why `__captured_*` remains the goal.

See `../README.md` for the capture + redaction procedure.
