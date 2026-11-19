# RazorpayX payout-webhook corpus — status (SVC-459)

## Current state (docs-sourced)

6 payloads landed from the Razorpay public docs, one per state in the
payout lifecycle:

- `payout-processed__*.json`
- `payout-failed__*.json`
- `payout-reversed__*.json`
- `payout-initiated__*.json`
- `payout-queued__*.json`
- `payout-rejected__*.json`

The `__docs_<id>` suffix marks these as docs-sourced. Replace with
`__captured_<id>` when real traffic is available (blocked on SVC-229).

Schema note: the initial schema pinned `utr` and `reference_id` to
`string`, but the docs clearly show both can be `null` (e.g. `utr` is
null on `payout.initiated`, `payout.queued`, `payout.rejected`;
`reference_id` is null on all six docs examples). Schema widened to
`["string", "null"]` to match reality — the handler already treats both
as optional at the Pydantic layer.

## Still blocked

- **SVC-229 activation.** Captured production payloads supersede docs
  the moment one lands in CloudWatch. Until then, docs are the
  contract.

Redaction rules: see `../README.md`.
