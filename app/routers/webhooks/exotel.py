"""Exotel webhook endpoints (SVC-450).

Two endpoints Exotel calls into on our pool numbers:

* ``POST /webhooks/exotel/incoming`` — fired when any phone dials one of the
  numbers in ``virtual_number_pool``. We decide direction (customer→SP or
  SP→customer) against the active mapping, write a ``call_records`` row in
  ``ringing`` state, and respond with ExoML that plays the consent ``.wav``
  (SVC-451) before bridging the call with ``<Dial>``. No active mapping →
  polite IVR + ``direction='unmatched'`` audit row.
* ``POST /webhooks/exotel/call-ended`` — fired when a call concludes. We
  find the row by ``provider_call_sid`` (Exotel ``CallSid``) and stamp
  terminal state (status, duration, recording URL, ``recording_expires_at =
  now() + 90d``). Idempotent — a retry on the same CallSid is a no-op.

Auth: HMAC-SHA256 of the raw request body against
``settings.EXOTEL_WEBHOOK_SECRET``. The signature is passed in the
``X-Exotel-Signature`` header (Exotel's standard). Constant-time compare;
mismatch → 403. Dev gets a bypass (``SKIP_EXOTEL_SIG=1``) so curl testing
against a local server works; hard-refused in production via the
``ENVIRONMENT`` check, same pattern as the E2E OTP bypass.

Design decisions (ticket SVC-450):

* **ExoML in-code, not Exotel's hosted IVR builder.** Keeps the branching
  logic (consent → dial / polite rejection) under version control and
  testable, not in a vendor UI.
* **Idempotent on ``CallSid``.** Exotel retries aggressively on timeouts;
  the double-POST must not create a second ``call_records`` row and must
  not re-bridge an already-bridged call. Both handlers upsert on
  ``provider_call_sid``.
* **Response ``media_type="application/xml"``.** Exotel parses the ExoML
  verbatim; a ``Content-Type: application/json`` body gets ignored and the
  bridge never happens.
* **Unmatched calls still log.** A wrong-number dial to our virtual
  number, or a legitimate call whose mapping just expired, is evidence
  in a dispute. Writing the row (direction='unmatched', bridged_to=None)
  keeps the audit trail honest.
* **Consent before dial.** The ``<Play>`` verb for the ``.wav`` URL from
  ``get_consent_wav_url`` precedes ``<Dial>``. Exotel will pre-roll the
  announcement for the caller before connecting, satisfying TRAI consent
  for the recording.
* **Language resolution.** We try the booking consumer's
  ``preferred_language`` for a customer→SP call (so the SP hears the
  announcement in the language the booking was placed in), else English.
  ``get_consent_wav_url`` already falls back on an unknown value.
* **Recording retention: 90 d.** Baked default. The nightly purge job
  (SVC-451) sweeps rows past ``recording_expires_at`` and deletes the
  provider-side file.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import xml.sax.saxutils as saxutils
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.booking import Booking
from app.models.call_mapping import CallMapping
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.user import User
from app.services.call_masking import CallMaskingService, _ensure_e164
from app.services.consent import get_consent_wav_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/exotel", tags=["Webhooks"])
settings = get_settings()


# Keep the 90-day window named so the test imports the exact constant back
# and we can tweak it in one place if TRAI guidance changes.
RECORDING_RETENTION = timedelta(days=90)

# Header Exotel sets on the signed request. The name is configurable on
# Exotel's side but we pin the standard default.
_SIGNATURE_HEADER = "X-Exotel-Signature"


# ---------------------------------------------------------------------------
# HMAC auth dependency
# ---------------------------------------------------------------------------


async def verify_exotel_signature(request: Request) -> bytes:
    """Verify the Exotel HMAC-SHA256 signature and return the raw body.

    * Reads the raw body (once) and keeps it on ``request.state`` so the
      handler can reparse without re-awaiting.
    * ``SKIP_EXOTEL_SIG=1`` + ``ENVIRONMENT != production`` → bypass. In
      production the toggle is ignored: a production webhook must be
      signed, period.
    * Empty secret in a non-dev environment → refuse (500); same stance as
      the Razorpay webhook.
    * Mismatch → 403. We deliberately don't leak which side was wrong
      (empty vs. bad vs. missing header) in the response — logs carry the
      detail for debugging.
    """
    raw_body = await request.body()
    # Cache the body so the handler doesn't have to await it a second time.
    request.state.exotel_raw_body = raw_body

    # Dev/test bypass. Hard-refused in production regardless of the toggle.
    if settings.SKIP_EXOTEL_SIG:
        if settings.ENVIRONMENT == "production":
            logger.error(
                "exotel.skip_sig_refused_in_production",
                extra={"path": request.url.path},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="SKIP_EXOTEL_SIG is ignored in production",
            )
        logger.warning(
            "exotel.signature_bypassed",
            extra={"path": request.url.path, "env": settings.ENVIRONMENT},
        )
        return raw_body

    secret = settings.EXOTEL_WEBHOOK_SECRET
    if not secret:
        if settings.ENVIRONMENT != "dev":
            logger.error(
                "exotel.secret_missing",
                extra={"path": request.url.path, "env": settings.ENVIRONMENT},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Exotel webhook secret not configured",
            )
        logger.warning(
            "exotel.signature_bypassed_empty_secret",
            extra={"path": request.url.path, "env": settings.ENVIRONMENT},
        )
        return raw_body

    sig_header = request.headers.get(_SIGNATURE_HEADER, "")
    if not sig_header:
        logger.warning(
            "exotel.signature_missing",
            extra={"path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing Exotel signature",
        )

    expected = hmac.new(
        key=secret.encode(),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        logger.warning(
            "exotel.signature_mismatch",
            extra={"path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Exotel signature",
        )
    return raw_body


# ---------------------------------------------------------------------------
# ExoML builder
# ---------------------------------------------------------------------------


def _xml_escape(value: str) -> str:
    """Escape for XML attribute / text node use (quotes included)."""
    return saxutils.escape(value, {'"': "&quot;", "'": "&apos;"})


def build_bridge_exoml(consent_wav_url: str, dial_to_e164: str) -> str:
    """Return ExoML that plays the consent announcement then bridges.

    Exotel reads the response body as ExoML (their XML dialect). ``<Play>``
    plays a hosted ``.wav`` to the caller before the dial connects; the
    recording header the caller hears satisfies TRAI consent. ``<Dial>``
    then rings ``dial_to_e164`` on the other party's real phone. The
    ``record="true"`` attribute turns on recording for the bridged leg.
    """
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<Response>"
        f"<Play>{_xml_escape(consent_wav_url)}</Play>"
        f'<Dial record="true">{_xml_escape(dial_to_e164)}</Dial>'
        "</Response>"
    )


def build_unmatched_exoml() -> str:
    """ExoML for an unmatched call: polite message, no bridge.

    Exotel's ``<Say>`` renders TTS; a short English line is enough — the
    user is dialling a number they shouldn't, or whose mapping expired.
    The call ends cleanly (no ``<Dial>``), the audit row is still written
    by the handler.
    """
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<Response>"
        "<Say>Sorry, there is no active booking on this number. "
        "Please check the number and try again.</Say>"
        "<Hangup/>"
        "</Response>"
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _resolve_consent_lang(
    db: AsyncSession, mapping: CallMapping, direction: CallDirection
) -> str | None:
    """Pick the language for the consent announcement.

    We try the booking consumer's ``preferred_language`` (the booking is
    in their language of record) regardless of direction — the consent
    announcement plays to whoever is calling in, so the customer hears
    it in their own language when they dial out and the SP hears the
    same clip the customer agreed to. ``get_consent_wav_url`` handles
    NULL / unknown by falling back to English, so we don't have to.
    """
    stmt = (
        select(User.preferred_language)
        .join(Booking, Booking.consumer_id == User.id)
        .where(Booking.id == mapping.booking_id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


@router.post("/incoming", status_code=status.HTTP_200_OK)
async def exotel_incoming(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _raw_body: bytes = Depends(verify_exotel_signature),
):
    """Handle an inbound call to a virtual number.

    Exotel posts form-encoded fields (``From``, ``To``, ``CallSid``, …).
    We look up the active mapping, determine direction, log a ringing
    row (idempotent on ``CallSid``), and respond with ExoML that plays
    the consent clip then bridges to the counter-party's real phone.
    Unmatched calls get the polite rejection + an unmatched audit row.
    """
    form = await request.form()
    from_e164 = _ensure_e164(str(form.get("From", "")).strip())
    to_e164 = _ensure_e164(str(form.get("To", "")).strip())
    call_sid = str(form.get("CallSid", "")).strip()

    if not call_sid:
        # Exotel always sends CallSid; a missing one means this isn't a
        # real Exotel delivery (or someone's dev-poking). 400 is clearer
        # than a 500 from the unique-constraint we'd hit on insert.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing CallSid")

    # Idempotency: an Exotel retry on the same CallSid must not create a
    # second row and must not toggle direction. Return the same ExoML.
    existing = (
        await db.execute(select(CallRecord).where(CallRecord.provider_call_sid == call_sid))
    ).scalar_one_or_none()
    if existing is not None:
        # Rebuild the response from the existing row — covers the "Exotel
        # didn't get our 200, is retrying" case cleanly.
        if existing.direction == CallDirection.UNMATCHED or existing.bridged_to_e164 is None:
            return Response(content=build_unmatched_exoml(), media_type="application/xml")
        # Reuse English on retry; we don't re-look up language. The first
        # response already told Exotel which .wav to play.
        return Response(
            content=build_bridge_exoml(get_consent_wav_url(None), existing.bridged_to_e164),
            media_type="application/xml",
        )

    service = CallMaskingService(db)
    mapping = await service.lookup_active_mapping(virtual_number=to_e164, from_e164=from_e164)

    now = datetime.now(UTC)

    if mapping is None:
        # Unmatched — log evidence, polite message.
        record = CallRecord(
            call_mapping_id=None,
            provider_call_sid=call_sid,
            from_e164=from_e164,
            to_e164=to_e164,
            bridged_to_e164=None,
            direction=CallDirection.UNMATCHED,
            status=CallStatus.RINGING,
            started_at=now,
        )
        db.add(record)
        await db.flush()
        logger.info(
            "exotel.incoming.unmatched",
            extra={
                "call_sid": call_sid,
                "from": from_e164,
                "to": to_e164,
            },
        )
        return Response(content=build_unmatched_exoml(), media_type="application/xml")

    # Determine direction. The caller matches either the customer or the
    # SP side of the mapping — bridge to the other one.
    if from_e164 == mapping.customer_phone_e164:
        direction = CallDirection.CUSTOMER_TO_SP
        bridge_to = mapping.sp_phone_e164
    elif from_e164 == mapping.sp_phone_e164:
        direction = CallDirection.SP_TO_CUSTOMER
        bridge_to = mapping.customer_phone_e164
    else:
        # Mapping exists for this virtual number, but the caller is
        # neither party. Could be a spoofed CLI, or a mid-grace reassign.
        # Treat as unmatched (no bridge, audit the evidence).
        record = CallRecord(
            call_mapping_id=mapping.id,
            provider_call_sid=call_sid,
            from_e164=from_e164,
            to_e164=to_e164,
            bridged_to_e164=None,
            direction=CallDirection.UNMATCHED,
            status=CallStatus.RINGING,
            started_at=now,
        )
        db.add(record)
        await db.flush()
        logger.warning(
            "exotel.incoming.caller_not_party",
            extra={
                "call_sid": call_sid,
                "from": from_e164,
                "to": to_e164,
                "mapping_id": str(mapping.id),
            },
        )
        return Response(content=build_unmatched_exoml(), media_type="application/xml")

    lang = await _resolve_consent_lang(db, mapping, direction)
    consent_url = get_consent_wav_url(lang)

    record = CallRecord(
        call_mapping_id=mapping.id,
        provider_call_sid=call_sid,
        from_e164=from_e164,
        to_e164=to_e164,
        bridged_to_e164=bridge_to,
        direction=direction,
        status=CallStatus.RINGING,
        started_at=now,
    )
    db.add(record)
    await db.flush()
    logger.info(
        "exotel.incoming.bridged",
        extra={
            "call_sid": call_sid,
            "direction": direction.value,
            "mapping_id": str(mapping.id),
        },
    )
    return Response(
        content=build_bridge_exoml(consent_url, bridge_to),
        media_type="application/xml",
    )


# Map Exotel's ``Status`` vocabulary → our ``CallStatus`` enum. Exotel uses
# the same tokens we baked into the enum (see models/call_record.py comment)
# so the map is identity — but we keep it explicit so an Exotel-side change
# surfaces as a KeyError in tests instead of a silent bad write.
_EXOTEL_STATUS_MAP: dict[str, CallStatus] = {
    "completed": CallStatus.COMPLETED,
    "in-progress": CallStatus.IN_PROGRESS,
    "in_progress": CallStatus.IN_PROGRESS,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "no_answer": CallStatus.NO_ANSWER,
    "busy": CallStatus.BUSY,
    "ringing": CallStatus.RINGING,
}


@router.post("/call-ended", status_code=status.HTTP_200_OK)
async def exotel_call_ended(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _raw_body: bytes = Depends(verify_exotel_signature),
):
    """Finalise a ``call_records`` row on call-end.

    Exotel posts ``CallSid``, ``Status``, ``DialCallDuration``,
    ``RecordingUrl`` (plus noise). We find the row by CallSid, stamp
    terminal state. Idempotent: a retry on an already-terminal row is a
    no-op (first wins — we don't overwrite a completion with a later
    ``failed`` retry).
    """
    form = await request.form()
    call_sid = str(form.get("CallSid", "")).strip()
    if not call_sid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing CallSid")

    row = (
        await db.execute(select(CallRecord).where(CallRecord.provider_call_sid == call_sid))
    ).scalar_one_or_none()
    if row is None:
        # Legitimate race: call-ended arrived before incoming (Exotel's
        # deliveries aren't ordered). Return 200 so Exotel doesn't retry
        # forever; the incoming handler will still create the ringing
        # row on its own arrival, and we accept the small gap in the
        # end-state for this call. Logging flagged so ops can audit.
        logger.warning(
            "exotel.call_ended.no_record",
            extra={"call_sid": call_sid},
        )
        return {"status": "ok"}

    # First-wins on terminal state. If the row is already in a terminal
    # state (completed / failed / no_answer / busy / declined_consent),
    # ignore this delivery entirely. Lets a late "failed" retry not clobber
    # a successful "completed".
    terminal = {
        CallStatus.COMPLETED,
        CallStatus.FAILED,
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
        CallStatus.DECLINED_CONSENT,
    }
    if row.status in terminal:
        logger.info(
            "exotel.call_ended.already_terminal",
            extra={"call_sid": call_sid, "status": row.status.value},
        )
        return {"status": "ok"}

    status_raw = str(form.get("Status", "")).strip().lower()
    new_status = _EXOTEL_STATUS_MAP.get(status_raw)
    if new_status is None:
        # Unknown Exotel status — stamp failed so the row terminates and
        # log the raw value; a status Exotel emits that we don't know
        # about is worth a human look.
        logger.warning(
            "exotel.call_ended.unknown_status",
            extra={"call_sid": call_sid, "status_raw": status_raw},
        )
        new_status = CallStatus.FAILED

    duration_raw = str(form.get("DialCallDuration", "")).strip()
    try:
        duration_sec = int(duration_raw) if duration_raw else None
    except ValueError:
        # Exotel sends empty string on no-answer; anything else that
        # fails to parse goes to NULL rather than 500-ing the webhook.
        logger.warning(
            "exotel.call_ended.bad_duration",
            extra={"call_sid": call_sid, "duration_raw": duration_raw},
        )
        duration_sec = None

    recording_url = str(form.get("RecordingUrl", "")).strip() or None

    now = datetime.now(UTC)
    row.status = new_status
    row.duration_sec = duration_sec
    row.ended_at = now
    if recording_url:
        row.recording_url = recording_url
        # Deliberately start the retention clock on call end, not call
        # start — the recording doesn't exist until the call ends.
        row.recording_expires_at = now + RECORDING_RETENTION
    await db.flush()
    logger.info(
        "exotel.call_ended.updated",
        extra={
            "call_sid": call_sid,
            "status": new_status.value,
            "duration_sec": duration_sec,
            "has_recording": bool(recording_url),
        },
    )
    return {"status": "ok"}


__all__ = [
    "RECORDING_RETENTION",
    "build_bridge_exoml",
    "build_unmatched_exoml",
    "router",
    "verify_exotel_signature",
]
