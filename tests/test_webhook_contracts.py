"""
Webhook contract tests — SVC-458 (Razorpay), SVC-459 (RazorpayX), SVC-460 (MSG91).

The harness: discover every ``*.json`` in ``tests/fixtures/webhooks/<vendor>/``,
assert it validates against ``tests/fixtures/webhooks/<vendor>/schema.json``.
The schema pins only the fields ``app/services/*.py`` actually reads; vendor
extras are ignored. See ``tests/fixtures/webhooks/README.md`` for the capture
+ redaction procedure.

Why discover-then-validate instead of hand-wiring tests per payload:
captured payloads will trickle in over time (Vipul pulls them from
CloudWatch as they appear in beta). Hand-wiring means a new payload needs
a code change; discovery means "drop the file in the right folder, run the
suite". Zero-friction growth of the contract corpus.

Failure mode this test would catch: the vendor silently renames / drops a
field we consume. Today the only check is ``RazorpayWebhookPayload.model_validate(...)``
which raises 400 at the live webhook — the booking stays PENDING forever,
money is already charged, consumer sees "paying…" spinner. This test
catches the drift in CI the morning after the vendor pushed it, not two
days later when a support ticket arrives.

While captured corpora are empty, the three suites xfail with a clear
reason so CI stays green but the TODOs are visible in the test output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError, validate

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "webhooks"


def _schema_for(vendor: str) -> dict:
    schema_path = FIXTURES_ROOT / vendor / "schema.json"
    assert schema_path.is_file(), f"Missing {schema_path}"
    with schema_path.open() as fh:
        return json.load(fh)


def _captured_payloads(vendor: str) -> list[Path]:
    """Every ``*.json`` under ``fixtures/webhooks/<vendor>/`` that isn't the
    schema. Sorted for deterministic test ordering."""
    folder = FIXTURES_ROOT / vendor
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.json") if p.name != "schema.json")


def _payload_ids(paths: list[Path]) -> list[str]:
    """Readable pytest parameterisation ids."""
    return [p.name for p in paths]


# --- Razorpay Payments (SVC-458) ---------------------------------------------

_RAZORPAY_PAYLOADS = _captured_payloads("razorpay")


@pytest.mark.xfail(
    not _RAZORPAY_PAYLOADS,
    reason="SVC-458: no captured Razorpay payloads yet — see "
    "tests/fixtures/webhooks/razorpay/CAPTURE_TODO.md",
    strict=False,
)
@pytest.mark.parametrize(
    "payload_path",
    _RAZORPAY_PAYLOADS or [FIXTURES_ROOT / "razorpay" / "__placeholder__.json"],
    ids=_payload_ids(_RAZORPAY_PAYLOADS) or ["placeholder"],
)
def test_razorpay_webhook_payload_matches_schema(payload_path: Path):
    """A captured Razorpay webhook body must validate against our pinned
    schema. If this goes red: the vendor changed a field we consume.
    """
    if not payload_path.exists():
        pytest.xfail("No captured payloads yet (SVC-458).")
    with payload_path.open() as fh:
        payload = json.load(fh)

    schema = _schema_for("razorpay")
    try:
        validate(instance=payload, schema=schema)
    except ValidationError as err:
        pytest.fail(
            f"{payload_path.name} no longer matches "
            f"tests/fixtures/webhooks/razorpay/schema.json:\n"
            f"  path: {list(err.absolute_path)}\n"
            f"  message: {err.message}\n"
            f"If Razorpay changed a field we read, update the schema AND the "
            f"handler at app/services/payment_service.py::handle_webhook. "
            f"Do NOT loosen the schema to make this pass."
        )


# --- RazorpayX payouts (SVC-459, blocked on SVC-229) -------------------------

_RAZORPAYX_PAYLOADS = _captured_payloads("razorpayx")


@pytest.mark.xfail(
    not _RAZORPAYX_PAYLOADS,
    reason="SVC-459: blocked on SVC-229 RazorpayX activation — see "
    "tests/fixtures/webhooks/razorpayx/CAPTURE_TODO.md",
    strict=False,
)
@pytest.mark.parametrize(
    "payload_path",
    _RAZORPAYX_PAYLOADS or [FIXTURES_ROOT / "razorpayx" / "__placeholder__.json"],
    ids=_payload_ids(_RAZORPAYX_PAYLOADS) or ["placeholder"],
)
def test_razorpayx_payout_payload_matches_schema(payload_path: Path):
    """A captured RazorpayX payout webhook body must validate against the
    schema derived from RazorpayX docs. Once SVC-229 (activation) lands and
    the first beta payout fires, Vipul drops captured bodies into
    tests/fixtures/webhooks/razorpayx/ and the xfail flips to pass.
    """
    if not payload_path.exists():
        pytest.xfail("Blocked on SVC-229 (SVC-459).")
    with payload_path.open() as fh:
        payload = json.load(fh)

    schema = _schema_for("razorpayx")
    try:
        validate(instance=payload, schema=schema)
    except ValidationError as err:
        pytest.fail(
            f"{payload_path.name} no longer matches "
            f"tests/fixtures/webhooks/razorpayx/schema.json:\n"
            f"  path: {list(err.absolute_path)}\n"
            f"  message: {err.message}\n"
            f"Check the RazorpayX dashboard changelog, then update both schema "
            f"AND PayoutService.handle_payout_webhook. Do NOT loosen the schema."
        )


# --- MSG91 OTP (SVC-460) ------------------------------------------------------

_MSG91_PAYLOADS = _captured_payloads("msg91")


@pytest.mark.xfail(
    not _MSG91_PAYLOADS,
    reason="SVC-460: no captured MSG91 response bodies yet — see "
    "tests/fixtures/webhooks/msg91/CAPTURE_TODO.md",
    strict=False,
)
@pytest.mark.parametrize(
    "payload_path",
    _MSG91_PAYLOADS or [FIXTURES_ROOT / "msg91" / "__placeholder__.json"],
    ids=_payload_ids(_MSG91_PAYLOADS) or ["placeholder"],
)
def test_msg91_response_payload_matches_schema(payload_path: Path):
    """A captured MSG91 ``/api/v5/otp`` response body must validate against
    the shape MSG91Client.send_otp expects. The two silent-fail modes in
    reference_msg91_otp_gotchas.md (HTTP-200-with-error-body, Hindi error
    message) are what this test guards against — if MSG91 returns a success
    HTTP code with a shape we treat as a successful send, OTPs vanish and
    we don't notice.
    """
    if not payload_path.exists():
        pytest.xfail("No captured payloads yet (SVC-460).")
    with payload_path.open() as fh:
        payload = json.load(fh)

    schema = _schema_for("msg91")
    try:
        validate(instance=payload, schema=schema)
    except ValidationError as err:
        pytest.fail(
            f"{payload_path.name} no longer matches "
            f"tests/fixtures/webhooks/msg91/schema.json:\n"
            f"  path: {list(err.absolute_path)}\n"
            f"  message: {err.message}"
        )


# --- Smoke: schemas themselves load as valid JSON-Schema ---------------------


@pytest.mark.parametrize("vendor", ["razorpay", "razorpayx", "msg91"])
def test_vendor_schema_is_well_formed(vendor: str):
    """Independent of whether we've captured any payloads yet, each vendor's
    schema file must be parseable as JSON and shaped like a JSON-Schema. Guards
    against a typo in the schema breaking the whole suite silently."""
    from jsonschema import Draft7Validator

    schema = _schema_for(vendor)
    # Raises if the schema itself is malformed. No payload needed.
    Draft7Validator.check_schema(schema)
