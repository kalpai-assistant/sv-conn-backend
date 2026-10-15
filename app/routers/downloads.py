"""Public download-flow endpoints (SVC-161, SVC-162).

Backs the `/download` page on the web + the in-app version-check hook.

- ``POST /downloads/sms-link`` — visitor enters their phone on desktop,
  we SMS them a link to open the download page on their phone (SVC-161).
- ``GET  /downloads/latest.json`` — installed app polls this on cold
  start + foreground; compares to installed version, nudges or hard-
  blocks per platform. See SVC-162.

Public (no auth). SMS endpoint IP-rate-limited to prevent SMS abuse.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.app_version import AppPlatform, AppVersion
from app.schemas.downloads import LatestVersionResponse, PlatformVersion
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


# ---------------------------------------------------------------------------
# SVC-162: in-app version check
# ---------------------------------------------------------------------------


@router.get(
    "/latest.json",
    response_model=LatestVersionResponse,
    responses={503: {"description": "Version data not seeded — Alembic migration missing?"}},
)
async def get_latest_version(
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LatestVersionResponse:
    """Return the latest + minimum-supported version for each platform.

    Hit on every app cold start + foreground event. Cached at the CDN edge
    for 5 minutes so a burst of foregrounds after a network blip doesn't
    hammer the DB. Long enough to spare load; short enough that flipping
    ``min_supported_version`` during an incident propagates in minutes,
    not hours.

    503 rather than an empty response if seed rows are missing — that's a
    deployment bug (missing migration), and returning defaults would
    silently mask it.
    """
    result = await db.execute(select(AppVersion))
    rows = {row.platform: row for row in result.scalars().all()}

    if any(p not in rows for p in (AppPlatform.ANDROID, AppPlatform.PWA, AppPlatform.IOS)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="App version metadata is missing. Alembic migration may not have run.",
        )

    response.headers["Cache-Control"] = "public, max-age=300"
    return LatestVersionResponse(
        android=PlatformVersion.model_validate(rows[AppPlatform.ANDROID]),
        pwa=PlatformVersion.model_validate(rows[AppPlatform.PWA]),
        ios=PlatformVersion.model_validate(rows[AppPlatform.IOS]),
    )
