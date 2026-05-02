"""
SMS client abstraction following the Open/Closed + Interface Segregation principles.

To add a new SMS provider:
1. Subclass AbstractSMSClient and implement send_otp().
2. Register the new key in get_sms_client().
No existing code changes needed.
"""
from __future__ import annotations

import abc
import httpx
from app.config import get_settings

settings = get_settings()


class AbstractSMSClient(abc.ABC):
    """Interface every SMS provider must satisfy."""

    @abc.abstractmethod
    async def send_otp(self, phone: str, otp: str) -> None:
        """Send a one-time password to the given phone number."""


# ---------------------------------------------------------------------------
# MSG91 implementation
# ---------------------------------------------------------------------------

class MSG91Client(AbstractSMSClient):
    _BASE_URL = "https://api.msg91.com/api/v5/otp"

    async def send_otp(self, phone: str, otp: str) -> None:
        params = {
            "authkey": settings.MSG91_AUTH_KEY,
            "mobile": phone.lstrip("+"),
            "message": f"Your ServiceConnect OTP is {otp}. Valid for 10 minutes.",
            "sender": "SVCCNT",
            "route": "4",
            "template_id": settings.MSG91_TEMPLATE_ID,
            "otp": otp,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(self._BASE_URL, json=params, timeout=10)
            response.raise_for_status()


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
            "Body": f"Your ServiceConnect OTP is {otp}. Valid for 10 minutes.",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                data=data,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                timeout=10,
            )
            response.raise_for_status()


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
            f"Unknown SMS_PROVIDER '{provider}'. "
            f"Valid options: {list(_PROVIDER_MAP.keys())}"
        )
    return client_class()
