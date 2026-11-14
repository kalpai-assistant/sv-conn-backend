# TODO: capture real RazorpayX payout-webhook payloads (SVC-459)

**Blocked on SVC-229** (RazorpayX activation). Until activation lands,
no real payloads exist to capture. The contract test for RazorpayX
xfails with `pytest.mark.xfail(reason="SVC-459: blocked on SVC-229")`.

Once activation lands and the first few payouts fire in beta, pull one
of each from CloudWatch:

- `payout-processed__*.json`
- `payout-failed__*.json`
- `payout-reversed__*.json`

Redaction rules: see `../README.md`.
