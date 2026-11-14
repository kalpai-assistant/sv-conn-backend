"""
G9 of SVC-456 stability wave 1 — webhook signature "dev escape hatch" guard tests
(SVC-465).

Failure mode these tests would catch: a deploy ships with ``ENVIRONMENT=dev`` in
App Runner's SSM AND an empty ``RAZORPAY_WEBHOOK_SECRET`` /
``RAZORPAYX_WEBHOOK_SECRET``. The existing ``_verify_and_parse`` in
``app/routers/payments.py`` logs a warning and accepts the body in dev. Anyone on
the internet can POST a forged ``payment.captured`` (flips a booking to PAID) or
``payout.reversed`` (returns money).

The guard code already lives at ``app/routers/payments.py:149`` —
``if settings.ENVIRONMENT != "dev": raise HTTPException(500, ...)``. These tests
lock it in so a well-intentioned refactor can't drop it.

Covered:

* Empty secret + ``ENVIRONMENT=beta`` → 500 (payments + payouts).
* Empty secret + ``ENVIRONMENT=production`` → 500 (payments + payouts).
* Empty secret + ``ENVIRONMENT=dev`` → the warn-and-accept path still works
  (so local devs aren't forced to fake signatures).
* Non-empty secret + bad signature → 400, in every env.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.routers import payments as payments_router


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _razorpay_body() -> bytes:
    """A shape-correct Razorpay webhook body.

    We deliberately keep this minimal — this file tests the signature guard, not
    the handler's business logic. G2 (SVC-458) owns the payload-contract tests.
    """
    return json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_TEST000000001",
                        "order_id": "order_TEST0000001",
                        "status": "captured",
                        "amount": 10000,
                    }
                }
            },
        }
    ).encode()


def _razorpayx_body() -> bytes:
    return json.dumps(
        {
            "event": "payout.processed",
            "payload": {
                "payout": {
                    "entity": {
                        "id": "pout_TEST0000001",
                        "reference_id": "svc-payout-1",
                        "status": "processed",
                    }
                }
            },
        }
    ).encode()


# --- Dev escape hatch stays open in dev ---------------------------------------


async def test_razorpay_empty_secret_in_dev_still_accepts(client, monkeypatch):
    """Dev convenience: an empty secret + ENVIRONMENT=dev logs a warning and
    accepts the body (so local .env without secrets doesn't need every test to
    fake a signature). Guards against a future refactor that tightens dev too.
    """
    monkeypatch.setattr(payments_router.settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(payments_router.settings, "RAZORPAY_WEBHOOK_SECRET", "")

    response = await client.post(
        "/payments/webhook",
        content=_razorpay_body(),
        headers={"content-type": "application/json"},
    )
    # Dev + empty secret → signature check skipped. The body is handled; the
    # handler may 200 or may raise its own domain error (unknown payment id in
    # a fresh DB). Either way the signature guard did not trip.
    assert response.status_code != 400
    assert response.status_code != 500


# --- Dev escape hatch is slammed shut in non-dev ------------------------------


@pytest.mark.parametrize("env", ["beta", "staging", "production", "prod"])
async def test_razorpay_empty_secret_outside_dev_refuses_500(client, monkeypatch, env):
    """The core safety assertion: outside dev, an unconfigured
    RAZORPAY_WEBHOOK_SECRET means the route refuses with 500.

    Without this guard, an attacker could POST a forged ``payment.captured`` and
    flip a booking to PAID.
    """
    monkeypatch.setattr(payments_router.settings, "ENVIRONMENT", env)
    monkeypatch.setattr(payments_router.settings, "RAZORPAY_WEBHOOK_SECRET", "")

    response = await client.post(
        "/payments/webhook",
        content=_razorpay_body(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 500, (
        f"Signature guard broken: {env} + empty RAZORPAY_WEBHOOK_SECRET must 500. "
        f"Got {response.status_code}."
    )


@pytest.mark.parametrize("env", ["beta", "staging", "production", "prod"])
async def test_razorpayx_empty_secret_outside_dev_refuses_500(client, monkeypatch, env):
    """Same story for the RazorpayX payout webhook — different secret,
    different endpoint, same attack surface. A forged ``payout.reversed`` can
    return money; must not fire with no secret in non-dev.
    """
    monkeypatch.setattr(payments_router.settings, "ENVIRONMENT", env)
    monkeypatch.setattr(payments_router.settings, "RAZORPAYX_WEBHOOK_SECRET", "")

    response = await client.post(
        "/payments/payout-webhook",
        content=_razorpayx_body(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 500, (
        f"Signature guard broken: {env} + empty RAZORPAYX_WEBHOOK_SECRET must 500. "
        f"Got {response.status_code}."
    )


# --- Bad signatures are rejected regardless of env ----------------------------


@pytest.mark.parametrize("env", ["dev", "beta", "production"])
async def test_razorpay_bad_signature_returns_400(client, monkeypatch, env):
    """With a non-empty secret, a wrong X-Razorpay-Signature must 400 — the
    hmac.compare_digest check at ``_verify_and_parse``. Independent of env.
    """
    monkeypatch.setattr(payments_router.settings, "ENVIRONMENT", env)
    monkeypatch.setattr(
        payments_router.settings, "RAZORPAY_WEBHOOK_SECRET", "correct-horse-battery"
    )

    response = await client.post(
        "/payments/webhook",
        content=_razorpay_body(),
        headers={
            "content-type": "application/json",
            "X-Razorpay-Signature": "obviously-wrong",
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("env", ["dev", "beta", "production"])
async def test_razorpayx_bad_signature_returns_400(client, monkeypatch, env):
    monkeypatch.setattr(payments_router.settings, "ENVIRONMENT", env)
    monkeypatch.setattr(
        payments_router.settings, "RAZORPAYX_WEBHOOK_SECRET", "correct-horse-battery"
    )

    response = await client.post(
        "/payments/payout-webhook",
        content=_razorpayx_body(),
        headers={
            "content-type": "application/json",
            "X-Razorpay-Signature": "obviously-wrong",
        },
    )
    assert response.status_code == 400


# --- Correct signature is accepted --------------------------------------------


async def test_razorpay_correct_signature_accepts(client, monkeypatch):
    """Reference check — a correctly-signed body gets past the guard. If this
    goes red it means the HMAC comparison was subtly broken (e.g. digest encoding
    mismatch between hex-vs-base64, or a case-sensitivity bug).
    """
    secret = "correct-horse-battery"
    monkeypatch.setattr(payments_router.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(payments_router.settings, "RAZORPAY_WEBHOOK_SECRET", secret)

    body = _razorpay_body()
    response = await client.post(
        "/payments/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Razorpay-Signature": _sign(secret, body),
        },
    )
    # Not 400 (sig OK) and not 500 (secret set). Handler may still return a
    # domain error if the payment id isn't in the DB, which is not what this
    # test is checking.
    assert response.status_code != 400
    assert response.status_code != 500
