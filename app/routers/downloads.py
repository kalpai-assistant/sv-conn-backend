"""Public download-flow endpoints (SVC-161).

Backs the `/download` page on the web.

- ``POST /downloads/sms-link`` — visitor enters their phone on desktop,
  we SMS them a link to open the download page on their phone.

Public (no auth), IP-rate-limited to prevent SMS abuse. Reuses the same
MSG91 client that sends OTPs.

**The SMS endpoint is behind a feature flag** (`SMS_DOWNLOAD_LINK_ENABLED`)
because sending a URL through MSG91's OTP template produces user-visible
copy like "Your Eazyserv OTP is https://…" — misleading. Turn on ONLY
after a dedicated MSG91 template is approved (submit form → 1-3 business
days). While off, the endpoint returns 501 and the frontend falls back to
QR-code-only on desktop.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import get_settings
from app.utils.rate_limiter import ip_rate_limit
from app.utils.sms_client import get_sms_client

router = APIRouter(prefix="/downloads", tags=["Downloads"])
settings = get_settings()

# Same phone format the auth flow enforces. E.164 India: +91 followed by
# 10 digits, first digit 6-9 (Indian mobile prefix).
_PHONE_PATTERN = r"^\+91[6-9]\d{9}$"

# Where the SMS points visitors. Hardcoded because we run one production
# frontend domain; if that ever changes, becomes an env var.
_DOWNLOAD_URL = "https://eazyserv.com/download"


class SmsLinkRequest(BaseModel):
    phone: str = Field(pattern=_PHONE_PATTERN, description="E.164 Indian mobile number")


@router.post(
    "/sms-link",
    status_code=status.HTTP_204_NO_CONTENT,
    # 3 SMS per IP per hour. Tight because each SMS costs money and abuse
    # (spamming random phone numbers with our download link) is easy without
    # this. Real users only need one SMS.
    dependencies=[Depends(ip_rate_limit("download_sms", 3, 3600))],
    responses={
        501: {"description": "SMS delivery not yet enabled — use the QR code."},
    },
)
async def send_download_link_sms(payload: SmsLinkRequest) -> None:
    """SMS the download page URL to the phone number the visitor entered.

    Public. Rate-limited by IP (3/hour). No user record created — this is
    the pre-signup surface.

    Returns 501 when the feature flag is off, which is the default during
    beta until the MSG91 template is approved.
    """
    if not settings.SMS_DOWNLOAD_LINK_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SMS delivery is not enabled yet. Please use the QR code.",
        )

    sms = get_sms_client()
    # Once the dedicated template is approved, this call swaps to a
    # `send_transactional(template_id, {url: ...})` method that doesn't
    # abuse the OTP slot. For now, the flag prevents this from being
    # reached in prod.
    await sms.send_otp(payload.phone, _DOWNLOAD_URL)
