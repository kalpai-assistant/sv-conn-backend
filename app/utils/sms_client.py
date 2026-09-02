"""
SMS client abstraction following the Open/Closed + Interface Segregation principles.

To add a new SMS provider:
1. Subclass AbstractSMSClient and implement send_otp().
2. Register the new key in get_sms_client().
No existing code changes needed.
"""

from __future__ import annotations

import abc
import logging

import httpx

from app.config import get_settings
from app.exceptions import SMSDeliveryError

settings = get_settings()
logger = logging.getLogger(__name__)


class AbstractSMSClient(abc.ABC):
    """Interface every SMS provider must satisfy."""

    @abc.abstractmethod
    async def send_otp(self, phone: str, otp: str) -> None:
        """Send a one-time password to the given phone number.

        Implementations MUST raise `SMSDeliveryError` when the provider
        reports a failure. Silent success on an actually-dropped SMS is a
        production incident waiting to happen (SVC-215).
        """


# ---------------------------------------------------------------------------
# MSG91 implementation (v5 OTP API)
# ---------------------------------------------------------------------------
#
# MSG91 v5 quirks that bit us in prod:
#
# 1. The API always returns HTTP 200 for well-formed requests, even when
#    the SMS is going to be dropped downstream. Success/failure lives in
#    the JSON body's `type` field ("success" | "error"), NOT the HTTP
#    status. So `raise_for_status()` alone is worse than useless — it
#    lulls the caller into thinking everything worked.
#
# 2. The v5 OTP endpoint takes just three inputs from us: `template_id`
#    (DLT-approved), `mobile`, and the template variables (e.g. `var1`
#    for a template body of "Dear User, please use this OTP: ##var1##").
#    The v3 params (`sender`, `route`, `message`) are IGNORED — the
#    actual SMS text comes from the DLT-registered template on MSG91's
#    side. Sending them is harmless noise but obscures what's actually
#    controlling delivery.
#
# 3. Authkey belongs in the `authkey` request header, not the JSON body.
#    Both are accepted, but the header is what MSG91's docs standardize
#    on and what our observability tools redact by name (see
#    logging_config.py). Putting a live secret in the body would slip
#    past that redaction if we ever start logging request bodies.
#
# 4. India requires every sender ID + template combination to be pre-
#    registered on a DLT (Distributed Ledger Technology) platform. An
#    empty or unapproved `template_id` gets `type=error` in the body OR
#    silent telco-side drop even when MSG91 says `type=success`. There
#    is no in-band way to detect the second case; that's what the MSG91
#    dashboard's Delivery Reports are for.


class MSG91Client(AbstractSMSClient):
    _BASE_URL = "https://api.msg91.com/api/v5/otp"

    async def send_otp(self, phone: str, otp: str) -> None:
        # `template_id` is required for DLT. Refuse to call MSG91 without
        # one — the API would still return 200 "success" and the SMS would
        # be silently dropped by the telco. Better to fail fast + loud so
        # ops sees the config gap.
        if not settings.MSG91_TEMPLATE_ID:
            raise SMSDeliveryError(
                "MSG91_TEMPLATE_ID is not configured; refusing to send. "
                "Set it in SSM once the DLT template is approved on MSG91."
            )

        # MSG91's /api/v5/otp endpoint expects the OTP under the `otp`
        # key, not `var1` — the template placeholder is `##OTP##`
        # (MSG91's reserved OTP-specific token), not `##var1##`. Passing
        # `var1` here is silently ignored; MSG91 then auto-generates its
        # own OTP into ##OTP## and sends THAT, leaving Redis and the
        # phone out of sync and every verify failing. This tripped us on
        # 2026-09-02 the moment DLT delivery finally worked end-to-end
        # for the first time (see SVC-225 saga).
        body = {
            "template_id": settings.MSG91_TEMPLATE_ID,
            "mobile": phone.lstrip("+"),
            "otp": otp,
        }
        headers = {
            "authkey": settings.MSG91_AUTH_KEY,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(self._BASE_URL, json=body, headers=headers)

        # HTTP-level failures (5xx from MSG91's edge, timeouts, DNS) —
        # rare in practice but worth surfacing as-is instead of falling
        # through to the JSON-body branch which would crash on non-JSON.
        if response.status_code >= 500:
            raise SMSDeliveryError(f"MSG91 responded {response.status_code}: {response.text[:200]}")

        # MSG91's real success signal lives in the body regardless of the
        # HTTP status. If the JSON is malformed we treat that as a
        # provider failure too — we can't verify delivery either way.
        try:
            payload = response.json()
        except ValueError as e:
            raise SMSDeliveryError(
                f"MSG91 returned non-JSON body (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            ) from e

        if payload.get("type") != "success":
            # Log the full response so the "why" is in CloudWatch, not
            # just "MSG91 said no". Common values for `message`:
            # invalid authkey / template not approved / DND blocked.
            raise SMSDeliveryError(f"MSG91 rejected send: {payload.get('message', payload)}")


# ---------------------------------------------------------------------------
# Twilio implementation
# ---------------------------------------------------------------------------


class TwilioClient(AbstractSMSClient):
    async def send_otp(self, phone: str, otp: str) -> None:
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.TWILIO_ACCOUNT_SID}/Messages.json"
        )
        data = {
            "From": settings.TWILIO_PHONE_NUMBER,
            "To": phone,
            "Body": f"Your Eazyserv OTP is {otp}. Valid for 10 minutes.",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                data=data,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                timeout=10,
            )
        # Twilio, unlike MSG91, does return a real HTTP status for
        # provider-side failures — a non-2xx here means the send did not
        # queue. Convert to the domain exception so the router surfaces
        # 502 to the client, same as MSG91.
        if response.status_code >= 400:
            raise SMSDeliveryError(
                f"Twilio responded {response.status_code}: {response.text[:200]}"
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDER_MAP: dict[str, type[AbstractSMSClient]] = {
    "msg91": MSG91Client,
    "twilio": TwilioClient,
}


def get_sms_client() -> AbstractSMSClient:
    """Return the configured SMS client. Raises ValueError for unknown providers."""
    provider = settings.SMS_PROVIDER.lower()
    client_class = _PROVIDER_MAP.get(provider)
    if client_class is None:
        raise ValueError(
            f"Unknown SMS_PROVIDER '{provider}'. " f"Valid options: {list(_PROVIDER_MAP.keys())}"
        )
    return client_class()
