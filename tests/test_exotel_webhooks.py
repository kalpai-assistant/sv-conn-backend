"""SVC-450: Exotel webhook endpoints (incoming + call-ended).

Six surfaces, six sections:

* HMAC signature verification — valid / invalid / missing / empty-secret
  in dev vs. non-dev / SKIP_EXOTEL_SIG bypass + production refusal.
* Incoming match → ExoML with <Play> consent + <Dial> to the right party
  (customer→SP vs. SP→customer).
* Incoming no-match → polite IVR + ``direction='unmatched'`` audit row.
* Idempotency on ``CallSid`` for both handlers.
* Call-ended terminal-state update + ``recording_expires_at = now()+90d``.
* Order-agnostic deliveries (call-ended-before-incoming is tolerated).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.call_mapping import CallMapping
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.virtual_number_pool import VirtualNumberPool, VirtualNumberStatus
from app.routers.webhooks.exotel import RECORDING_RETENTION
from app.services.consent import CONSENT_WAV_URLS
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

pytestmark = pytest.mark.asyncio

settings = get_settings()

ADDRESS_POINT = "POINT(72.8777 19.0760)"
INCOMING_URL = "/webhooks/exotel/incoming"
ENDED_URL = "/webhooks/exotel/call-ended"
TEST_SECRET = "test-exotel-secret-value"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_e164() -> str:
    """Unique Indian-mobile-shape E.164. Same shape SVC-449/451 use."""
    n = uuid.uuid4().int % 10**9
    return f"+9180{n:010d}"[:16]


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _form_body(fields: dict[str, str]) -> bytes:
    """Encode an Exotel-style form POST body."""
    return urlencode(fields).encode()


def _headers(body: bytes, *, signed: bool = True, secret: str = TEST_SECRET) -> dict:
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        h["X-Exotel-Signature"] = _sign(body, secret)
    return h


async def _seed_number(db) -> VirtualNumberPool:
    n = VirtualNumberPool(
        id=uuid.uuid4(),
        e164=_unique_e164(),
        status=VirtualNumberStatus.ACTIVE,
    )
    db.add(n)
    await db.flush()
    return n


async def _seed_mapping(
    db,
    *,
    customer_phone: str | None = None,
    sp_phone: str | None = None,
    consumer_lang: str | None = None,
) -> tuple[CallMapping, Address]:
    """Build consumer + provider + booking + an active mapping.

    Returns the mapping and the consumer's address (callers rarely need the
    address, but we return it so the surrounding bookings can resolve).
    """
    consumer = await UserFactory.create(db, role="consumer")
    if consumer_lang is not None:
        consumer.preferred_language = consumer_lang
    provider = await UserFactory.create(db, role="provider")
    address = Address(
        user_id=consumer.id,
        label="home",
        full_address="12 MG Road",
        location=ADDRESS_POINT,
    )
    db.add(address)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=address.id,
        status=BookingStatus.ACCEPTED,
    )
    number = await _seed_number(db)

    def _e164(p: str) -> str:
        return p if p.startswith("+") else "+" + p

    mapping = CallMapping(
        id=uuid.uuid4(),
        virtual_number_id=number.id,
        virtual_number_e164=number.e164,
        booking_id=booking.id,
        customer_phone_e164=_e164(customer_phone or consumer.phone),
        sp_phone_e164=_e164(sp_phone or provider.phone),
        allocated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=7),
        released_at=None,
    )
    db.add(mapping)
    await db.flush()
    return mapping, address


# ---------------------------------------------------------------------------
# HMAC signature
# ---------------------------------------------------------------------------


async def test_rejects_unsigned_request_in_non_dev(client):
    body = _form_body({"From": "+911", "To": "+912", "CallSid": "CAxx"})
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(
            INCOMING_URL,
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    # Missing header → 403.
    assert resp.status_code == 403


async def test_rejects_bad_signature(client):
    body = _form_body({"From": "+911", "To": "+912", "CallSid": "CAyy"})
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(
            INCOMING_URL,
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Exotel-Signature": "deadbeef" * 8,
            },
        )
    assert resp.status_code == 403


async def test_refuses_empty_secret_in_non_dev(client):
    body = _form_body({"From": "+911", "To": "+912", "CallSid": "CAzz"})
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", ""),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "production"),
    ):
        resp = await client.post(
            INCOMING_URL,
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 500


async def test_skip_sig_bypass_in_dev(client, db_session):
    """SKIP_EXOTEL_SIG=1 in dev skips the HMAC check end-to-end."""
    body = _form_body({"From": "+919999999999", "To": "+919000000000", "CallSid": "CAdev"})
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", True),
        patch.object(settings, "ENVIRONMENT", "dev"),
    ):
        resp = await client.post(
            INCOMING_URL,
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            # Deliberately no X-Exotel-Signature header.
        )
    # No mapping → unmatched, but a 200 proves the HMAC dep accepted the call.
    assert resp.status_code == 200


async def test_skip_sig_refused_in_production(client):
    """SKIP_EXOTEL_SIG=1 is ignored in production — a prod webhook MUST be signed."""
    body = _form_body({"From": "+911", "To": "+912", "CallSid": "CAprod"})
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", True),
        patch.object(settings, "ENVIRONMENT", "production"),
    ):
        resp = await client.post(
            INCOMING_URL,
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Incoming — match → bridge
# ---------------------------------------------------------------------------


async def test_incoming_customer_to_sp_bridges_to_sp(client, db_session):
    mapping, _ = await _seed_mapping(db_session)

    body = _form_body(
        {
            "From": mapping.customer_phone_e164,
            "To": mapping.virtual_number_e164,
            "CallSid": "CAcust2sp",
            "CallType": "call-attempt",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(INCOMING_URL, content=body, headers=_headers(body))

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    xml = resp.text
    # <Play> for the consent WAV precedes <Dial> (so TRAI consent plays
    # before the bridge connects).
    assert xml.index("<Play>") < xml.index("<Dial")
    assert "en.wav" in xml  # default lang fallback
    # Bridged to the SP's real number, not the customer.
    assert mapping.sp_phone_e164 in xml
    assert mapping.customer_phone_e164 not in xml
    assert 'record="true"' in xml

    # call_records row created in RINGING, customer→SP direction.
    rec = (
        await db_session.execute(
            select(CallRecord).where(CallRecord.provider_call_sid == "CAcust2sp")
        )
    ).scalar_one()
    assert rec.direction == CallDirection.CUSTOMER_TO_SP
    assert rec.status == CallStatus.RINGING
    assert rec.bridged_to_e164 == mapping.sp_phone_e164
    assert rec.call_mapping_id == mapping.id


async def test_incoming_sp_to_customer_bridges_to_customer(client, db_session):
    mapping, _ = await _seed_mapping(db_session)

    body = _form_body(
        {
            "From": mapping.sp_phone_e164,
            "To": mapping.virtual_number_e164,
            "CallSid": "CAsp2cust",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(INCOMING_URL, content=body, headers=_headers(body))

    assert resp.status_code == 200
    xml = resp.text
    assert mapping.customer_phone_e164 in xml
    assert mapping.sp_phone_e164 not in xml

    rec = (
        await db_session.execute(
            select(CallRecord).where(CallRecord.provider_call_sid == "CAsp2cust")
        )
    ).scalar_one()
    assert rec.direction == CallDirection.SP_TO_CUSTOMER
    assert rec.bridged_to_e164 == mapping.customer_phone_e164


async def test_incoming_uses_consumer_preferred_language(client, db_session):
    """The booking consumer's preferred_language picks the consent .wav."""
    mapping, _ = await _seed_mapping(db_session, consumer_lang="hi")
    await db_session.flush()

    body = _form_body(
        {
            "From": mapping.customer_phone_e164,
            "To": mapping.virtual_number_e164,
            "CallSid": "CAhi",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(INCOMING_URL, content=body, headers=_headers(body))
    assert resp.status_code == 200
    assert CONSENT_WAV_URLS["hi"] in resp.text


# ---------------------------------------------------------------------------
# Incoming — unmatched
# ---------------------------------------------------------------------------


async def test_incoming_unmatched_logs_record_and_returns_polite_ivr(client, db_session):
    # No mapping seeded. Any inbound call → unmatched.
    body = _form_body(
        {
            "From": "+919111111111",
            "To": _unique_e164(),
            "CallSid": "CAunmatch",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(INCOMING_URL, content=body, headers=_headers(body))

    assert resp.status_code == 200
    assert "<Say>" in resp.text
    assert "<Dial" not in resp.text  # explicitly no bridge
    assert "<Hangup" in resp.text

    rec = (
        await db_session.execute(
            select(CallRecord).where(CallRecord.provider_call_sid == "CAunmatch")
        )
    ).scalar_one()
    assert rec.direction == CallDirection.UNMATCHED
    assert rec.bridged_to_e164 is None
    assert rec.call_mapping_id is None


async def test_incoming_wrong_party_is_unmatched_with_mapping_id(client, db_session):
    """Mapping exists but caller is neither party → unmatched, still audited."""
    mapping, _ = await _seed_mapping(db_session)

    body = _form_body(
        {
            "From": "+919999999999",  # not customer, not SP
            "To": mapping.virtual_number_e164,
            "CallSid": "CAstranger",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(INCOMING_URL, content=body, headers=_headers(body))

    assert resp.status_code == 200
    assert "<Dial" not in resp.text

    rec = (
        await db_session.execute(
            select(CallRecord).where(CallRecord.provider_call_sid == "CAstranger")
        )
    ).scalar_one()
    assert rec.direction == CallDirection.UNMATCHED
    # We still attach the mapping id (evidence: this DID hit a live mapping).
    assert rec.call_mapping_id == mapping.id


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_incoming_idempotent_on_call_sid(client, db_session):
    """Exotel retry on the same CallSid must not create a second row."""
    mapping, _ = await _seed_mapping(db_session)

    body = _form_body(
        {
            "From": mapping.customer_phone_e164,
            "To": mapping.virtual_number_e164,
            "CallSid": "CAretry",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        r1 = await client.post(INCOMING_URL, content=body, headers=_headers(body))
        r2 = await client.post(INCOMING_URL, content=body, headers=_headers(body))

    assert r1.status_code == 200 and r2.status_code == 200
    # Second response is also an ExoML with the same bridge target.
    assert mapping.sp_phone_e164 in r2.text

    rows = (
        (
            await db_session.execute(
                select(CallRecord).where(CallRecord.provider_call_sid == "CAretry")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Call-ended
# ---------------------------------------------------------------------------


async def test_call_ended_stamps_terminal_state_and_retention(client, db_session):
    mapping, _ = await _seed_mapping(db_session)

    inc_body = _form_body(
        {
            "From": mapping.customer_phone_e164,
            "To": mapping.virtual_number_e164,
            "CallSid": "CAend1",
        }
    )
    end_body = _form_body(
        {
            "CallSid": "CAend1",
            "Status": "completed",
            "DialCallDuration": "42",
            "RecordingUrl": "https://exotel.example/Recordings/abc123.mp3",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        await client.post(INCOMING_URL, content=inc_body, headers=_headers(inc_body))
        before = datetime.now(UTC)
        end_resp = await client.post(ENDED_URL, content=end_body, headers=_headers(end_body))

    assert end_resp.status_code == 200
    rec = (
        await db_session.execute(select(CallRecord).where(CallRecord.provider_call_sid == "CAend1"))
    ).scalar_one()
    await db_session.refresh(rec)
    assert rec.status == CallStatus.COMPLETED
    assert rec.duration_sec == 42
    assert rec.recording_url == "https://exotel.example/Recordings/abc123.mp3"
    assert rec.recording_expires_at is not None
    # 90-day retention, give or take a few seconds for the test clock.
    delta = rec.recording_expires_at - before
    assert abs((delta - RECORDING_RETENTION).total_seconds()) < 5
    assert rec.ended_at is not None


async def test_call_ended_without_recording_leaves_retention_null(client, db_session):
    """No-answer / busy calls never recorded → recording_expires_at stays NULL."""
    mapping, _ = await _seed_mapping(db_session)

    inc_body = _form_body(
        {
            "From": mapping.customer_phone_e164,
            "To": mapping.virtual_number_e164,
            "CallSid": "CAnoans",
        }
    )
    end_body = _form_body(
        {
            "CallSid": "CAnoans",
            "Status": "no-answer",
            "DialCallDuration": "",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        await client.post(INCOMING_URL, content=inc_body, headers=_headers(inc_body))
        await client.post(ENDED_URL, content=end_body, headers=_headers(end_body))

    rec = (
        await db_session.execute(
            select(CallRecord).where(CallRecord.provider_call_sid == "CAnoans")
        )
    ).scalar_one()
    await db_session.refresh(rec)
    assert rec.status == CallStatus.NO_ANSWER
    assert rec.duration_sec is None
    assert rec.recording_url is None
    assert rec.recording_expires_at is None


async def test_call_ended_idempotent_on_terminal_row(client, db_session):
    """A late Exotel retry after a completed row is a no-op (first wins)."""
    mapping, _ = await _seed_mapping(db_session)
    inc_body = _form_body(
        {
            "From": mapping.customer_phone_e164,
            "To": mapping.virtual_number_e164,
            "CallSid": "CAterm",
        }
    )
    end_ok = _form_body(
        {
            "CallSid": "CAterm",
            "Status": "completed",
            "DialCallDuration": "60",
            "RecordingUrl": "https://exotel.example/Recordings/final.mp3",
        }
    )
    end_fail = _form_body({"CallSid": "CAterm", "Status": "failed", "DialCallDuration": "0"})
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        await client.post(INCOMING_URL, content=inc_body, headers=_headers(inc_body))
        await client.post(ENDED_URL, content=end_ok, headers=_headers(end_ok))
        resp2 = await client.post(ENDED_URL, content=end_fail, headers=_headers(end_fail))

    assert resp2.status_code == 200
    rec = (
        await db_session.execute(select(CallRecord).where(CallRecord.provider_call_sid == "CAterm"))
    ).scalar_one()
    await db_session.refresh(rec)
    # Still COMPLETED, not FAILED — first terminal write wins.
    assert rec.status == CallStatus.COMPLETED
    assert rec.duration_sec == 60


async def test_call_ended_before_incoming_returns_ok(client, db_session):
    """call-ended with no matching ringing row still 200s (race-tolerant)."""
    end_body = _form_body(
        {
            "CallSid": "CAghost",
            "Status": "failed",
            "DialCallDuration": "0",
        }
    )
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(ENDED_URL, content=end_body, headers=_headers(end_body))
    assert resp.status_code == 200


async def test_call_ended_missing_call_sid_is_400(client):
    body = _form_body({"Status": "completed"})
    with (
        patch.object(settings, "EXOTEL_WEBHOOK_SECRET", TEST_SECRET),
        patch.object(settings, "SKIP_EXOTEL_SIG", False),
        patch.object(settings, "ENVIRONMENT", "staging"),
    ):
        resp = await client.post(ENDED_URL, content=body, headers=_headers(body))
    assert resp.status_code == 400
