# TODO: capture real Razorpay webhook payloads (SVC-458)

The harness (`tests/test_webhook_contracts.py`) + schema
(`schema.json`) ship empty. The contract tests xfail with
`pytest.mark.xfail(reason="SVC-458: no captured payloads yet")` until this
directory contains at least one `*.json` per event-kind we care about:

- `payment-captured__*.json`
- `payment-failed__*.json`
- `payment-dispute-created__*.json`
- `refund-processed__*.json`
- `settlement-processed__*.json`

See `../README.md` for the capture + redaction procedure.

Pull-request owner: Vipul (CloudWatch access).
