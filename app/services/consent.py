"""Consent IVR URLs, DNC check, Exotel recording-delete wrapper (SVC-451).

Three things live here, all small, all testable in isolation:

* :func:`get_consent_wav_url` — returns the Exotel-media-library URL of
  the consent IVR ``.wav`` for a given language, with an English
  fallback. Called by the Exotel ``incoming`` webhook (SVC-450) to
  render the ``<Play>`` verb before ``<Dial>``.
* :func:`check_dnc` / :func:`assert_not_on_dnc` — DNC pre-check used by
  :meth:`CallMaskingService.allocate_mapping` (SVC-449) to refuse
  allocation when either party is opted out.
* :class:`ExotelRecordingClient` — thin wrapper around the Exotel
  delete-recording API. Mockable: the real HTTP client is only built
  inside the context manager, and the nightly purge job
  (``app.jobs.purge_expired_recordings``) accepts an injected client so
  tests exercise the full code path without network. SVC-447 (Exotel KYC)
  blocks real E2E; placeholder credentials until it lands.

Baked defaults (SVC-451):

* Three supported languages: ``en`` / ``hi`` / ``mr``. Anything else
  falls back to ``en`` — same set as :data:`SUPPORTED_LANGUAGES` on
  ``users.preferred_language``.
* The ``.wav`` URLs are **placeholders** keyed off a ``settings.``-style
  base. SVC-447 will upload the real recordings to the Exotel media
  library; swapping the constants here is a one-liner once the final
  URLs are known.
* DNC check is a **block**, not a warning. A hit raises
  :class:`PhoneOnDoNotCallList` (422). No self-serve opt-out path.
"""

from __future__ import annotations

import logging
from typing import Literal, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import PhoneOnDoNotCallList
from app.models.dnc_list import DncListEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Consent IVR URLs
# ---------------------------------------------------------------------------


# Supported language codes for the consent IVR. Matches
# ``app.models.user.SUPPORTED_LANGUAGES`` by design — a user's
# ``preferred_language`` flows straight through to this map.
SupportedLang = Literal["en", "hi", "mr"]
_SUPPORTED_LANGS: tuple[SupportedLang, ...] = ("en", "hi", "mr")
_DEFAULT_LANG: SupportedLang = "en"


# Placeholder consent ``.wav`` URLs. SVC-447 will upload the real
# recordings to Exotel's media library and these constants get swapped for
# the final signed URLs. Keep the shape stable so SVC-450's webhook code
# does not have to change.
#
# Why module constants (not Settings)? The URLs are static — once Exotel
# serves them they don't rotate — and keeping them here means the webhook
# code reads them with zero config plumbing, and tests don't need to patch
# a Settings subclass.
CONSENT_WAV_URLS: dict[SupportedLang, str] = {
    "en": "https://exotel-placeholder.invalid/consent/en.wav",
    "hi": "https://exotel-placeholder.invalid/consent/hi.wav",
    "mr": "https://exotel-placeholder.invalid/consent/mr.wav",
}


def get_consent_wav_url(lang: str | None) -> str:
    """Return the consent IVR ``.wav`` URL for ``lang``, English on miss.

    The press-1-to-continue / press-2-to-decline IVR played by the
    Exotel ``incoming`` webhook (SVC-450) before the ``<Dial>`` verb.

    ``lang`` is tolerant: ``None``, unknown, or mis-cased values fall
    back to ``en``. Caller (SVC-450) determines ``lang`` via the booking
    or user's ``preferred_language`` and passes it in; the fallback here
    is defence in depth.
    """
    if lang:
        normalized = lang.strip().lower()
        if normalized in CONSENT_WAV_URLS:
            return CONSENT_WAV_URLS[normalized]  # type: ignore[index]
    return CONSENT_WAV_URLS[_DEFAULT_LANG]


# ---------------------------------------------------------------------------
# DNC check
# ---------------------------------------------------------------------------


async def check_dnc(db: AsyncSession, phone_e164: str) -> bool:
    """Return True if ``phone_e164`` is on the DNC list.

    Single indexed read against ``dnc_list.phone_e164`` (UNIQUE). Does
    NOT raise; callers that want the block-and-surface behaviour should
    use :func:`assert_not_on_dnc`.
    """
    stmt = select(DncListEntry.id).where(DncListEntry.phone_e164 == phone_e164)
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def assert_not_on_dnc(db: AsyncSession, phone_e164: str, *, party: str) -> None:
    """Raise :class:`PhoneOnDoNotCallList` if ``phone_e164`` is on DNC.

    ``party`` is one of ``"customer"`` / ``"sp"`` — included in the log
    record so ops know which side blocked the mapping (useful when
    debugging: a hit on the SP side almost always means a provider needs
    to be re-onboarded, a hit on the customer side means the user
    requested opt-out via support).
    """
    if await check_dnc(db, phone_e164):
        logger.warning(
            "call_masking.dnc_block",
            extra={"party": party, "phone_e164": phone_e164},
        )
        raise PhoneOnDoNotCallList()


# ---------------------------------------------------------------------------
# Exotel recording-delete client (mockable)
# ---------------------------------------------------------------------------


class RecordingDeleteClient(Protocol):
    """Minimal surface the purge job calls.

    Keeping this a Protocol (not a concrete class) means tests can supply
    a bare mock without inheriting from the real Exotel client. The real
    implementation is :class:`ExotelRecordingClient` below.
    """

    async def delete_recording(self, recording_url: str) -> None:
        """Delete the recording at ``recording_url`` on the provider side.

        Raises on API error. The purge job treats a raise as "skip this
        row, keep ``recording_url`` set, log loudly" — a failure must
        NOT clear the URL, or we'd lose the pointer to a recording we
        never actually deleted.
        """
        ...


class ExotelRecordingClient:
    """Thin wrapper around the Exotel delete-recording API.

    Real credentials live in ``settings.EXOTEL_*`` once SVC-447 lands;
    until then the client is only wired up in production code paths and
    tests always inject a mock. The call surface is intentionally one
    method so the mock is one line.

    Exotel API reference (as of 2025):
    ``DELETE https://{sid}:{token}@api.exotel.com/v1/Accounts/{sid}/Recordings/{recording_sid}.json``

    The ``recording_url`` stored on ``call_records`` is a signed URL
    whose path ends in ``.../Recordings/{recording_sid}.mp3`` (or
    similar). We parse the SID off the last path segment rather than
    carrying a separate column — SVC-448 deliberately stored a URL, not
    a SID, so the admin console can play it back without a lookup.
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._timeout = timeout

    async def delete_recording(self, recording_url: str) -> None:
        recording_sid = _extract_recording_sid(recording_url)
        delete_url = (
            f"https://api.exotel.com/v1/Accounts/{self._account_sid}"
            f"/Recordings/{recording_sid}.json"
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.delete(
                delete_url,
                auth=(self._account_sid, self._auth_token),
            )
            # Exotel returns 204 on success, 404 if the recording is
            # already gone (which we treat as success for idempotency).
            if response.status_code not in (200, 204, 404):
                response.raise_for_status()


def _extract_recording_sid(recording_url: str) -> str:
    """Pull the recording SID off the tail of an Exotel URL.

    Exotel recording URLs end in ``.../Recordings/{sid}.{ext}``. We lop
    off the extension and any query string. Raises ``ValueError`` on an
    unrecognised shape so the purge job logs the bad URL and skips the
    row — better than nulling ``recording_url`` for a row whose recording
    we could not actually delete.
    """
    # Drop query string, then take the last path segment.
    path = recording_url.split("?", 1)[0]
    last = path.rsplit("/", 1)[-1]
    if "." in last:
        last = last.rsplit(".", 1)[0]
    if not last:
        raise ValueError(f"Cannot extract recording SID from {recording_url!r}")
    return last


__all__ = [
    "CONSENT_WAV_URLS",
    "ExotelRecordingClient",
    "RecordingDeleteClient",
    "assert_not_on_dnc",
    "check_dnc",
    "get_consent_wav_url",
]
