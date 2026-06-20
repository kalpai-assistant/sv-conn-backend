"""
RazorpayX payout client.

RazorpayX (payouts) is a separate product from Razorpay (payments) and is not
covered by the ``razorpay`` Python SDK, so payouts are made via direct REST calls
to the RazorpayX API using the same key/secret credentials.

The composite Payout API creates the contact, fund account, and payout in a single
call, so we don't need to pre-create and store contact/fund-account IDs.

NOTE (v1.1): the composite call creates a fresh RazorpayX contact + fund account on
every payout, which accumulates duplicate contacts over time. Acceptable for beta;
the v1.1 plan is to create+cache a contact id + fund account id once per provider.
"""
import asyncio
import logging

import httpx

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_PAYOUTS_URL = "https://api.razorpay.com/v1/payouts"

# RazorpayX outages / gateway errors are transient — retry with backoff. The
# X-Payout-Idempotency header makes a retry safe (no duplicate payout).
_RETRYABLE_STATUS = {502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5


async def create_payout(
    *,
    amount_paise: int,
    idempotency_key: str,
    reference_id: str,
    purpose: str = "payout",
    vpa: str | None = None,
    account_number: str | None = None,
    ifsc: str | None = None,
    contact_name: str = "Service Provider",
) -> dict:
    """
    Create a RazorpayX payout to a provider via the composite API.

    Pass either ``vpa`` (UPI) or ``account_number`` + ``ifsc`` (bank). The payout
    is funded from ``RAZORPAYX_ACCOUNT_NUMBER``. ``idempotency_key`` makes the
    call safe to retry — RazorpayX will not create a duplicate payout for the same
    key. Returns the RazorpayX payout entity (includes ``id`` and ``status``).
    """
    if vpa:
        fund_account = {
            "account_type": "vpa",
            "vpa": {"address": vpa},
        }
        mode = "UPI"
    elif account_number and ifsc:
        fund_account = {
            "account_type": "bank_account",
            "bank_account": {
                "name": contact_name,
                "account_number": account_number,
                "ifsc": ifsc,
            },
        }
        mode = "IMPS"
    else:
        raise ValueError("Payout requires a VPA or an account_number + IFSC")

    payload = {
        "account_number": settings.RAZORPAYX_ACCOUNT_NUMBER,
        "amount": amount_paise,
        "currency": "INR",
        "mode": mode,
        "purpose": purpose,
        # Echoed back on payout webhooks so we can map the payout to a provider.
        "reference_id": reference_id,
        "fund_account": {
            "contact": {"name": contact_name, "type": "vendor"},
            **fund_account,
        },
    }

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    _PAYOUTS_URL,
                    auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
                    headers={"X-Payout-Idempotency": idempotency_key},
                    json=payload,
                )
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            # Retry only transient failures: network errors, or 502/503/504 from
            # RazorpayX. Anything else (4xx, etc.) is a real error — raise at once.
            status_code = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            transient = status_code is None or status_code in _RETRYABLE_STATUS
            if not transient or attempt == _MAX_ATTEMPTS:
                raise
            backoff = _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            logger.warning(
                "RazorpayX payout transient failure (attempt %s/%s, status=%s); "
                "retrying in %ss",
                attempt, _MAX_ATTEMPTS, status_code, backoff,
            )
            await asyncio.sleep(backoff)
