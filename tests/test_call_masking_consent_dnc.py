"""SVC-451: consent-wav URL, DNC pre-allocation block, recording purge.

Three independent surfaces, three sections:

* Consent IVR helper — ``get_consent_wav_url(lang)`` returns the mapped
  URL for ``en``/``hi``/``mr`` and falls back to ``en`` for anything else.
* DNC pre-check — ``CallMaskingService.allocate_mapping`` refuses with
  :class:`PhoneOnDoNotCallList` when either party is on ``dnc_list``,
  and does NOT burn a pool number on the refused path.
* Recording purge — the async core of the nightly job calls the Exotel
  delete client for each expired row, clears ``recording_url`` on
  success, keeps it on failure, and never drops the ``call_records``
  row itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.exceptions import PhoneOnDoNotCallList
from app.jobs.purge_expired_recordings import purge_expired_recordings_async
from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.call_mapping import CallMapping
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.dnc_list import DncListEntry, DncSource
from app.models.virtual_number_pool import VirtualNumberPool, VirtualNumberStatus
from app.services.call_masking import CallMaskingService, _ensure_e164
from app.services.consent import (
    CONSENT_WAV_URLS,
    assert_not_on_dnc,
    check_dnc,
    get_consent_wav_url,
)
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

ADDRESS_POINT = "POINT(72.8777 19.0760)"

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _unique_e164() -> str:
    """Unique Indian-mobile-shape E.164 per seed, same convention as SVC-449."""
    n = uuid.uuid4().int % 10**9
    return f"+9180{n:010d}"[:16]


async def _seed_number(db) -> VirtualNumberPool:
    n = VirtualNumberPool(
        id=uuid.uuid4(),
        e164=_unique_e164(),
        status=VirtualNumberStatus.ACTIVE,
    )
    db.add(n)
    await db.flush()
    return n


async def _booking_pair(db):
    consumer = await UserFactory.create(db, role="consumer")
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
    return consumer, provider, booking


async def _add_to_dnc(db, phone_e164: str, source: DncSource = DncSource.USER_REQUEST):
    entry = DncListEntry(
        id=uuid.uuid4(),
        phone_e164=phone_e164,
        source=source,
        reason="test",
    )
    db.add(entry)
    await db.flush()
    return entry


# ---------------------------------------------------------------------------
# Consent IVR URL helper
# ---------------------------------------------------------------------------


def test_consent_wav_returns_mapped_url_for_each_supported_lang():
    for lang in ("en", "hi", "mr"):
        assert get_consent_wav_url(lang) == CONSENT_WAV_URLS[lang]


def test_consent_wav_falls_back_to_english_on_none():
    assert get_consent_wav_url(None) == CONSENT_WAV_URLS["en"]


def test_consent_wav_falls_back_on_unknown_lang():
    # Bangla (not supported) → en.
    assert get_consent_wav_url("bn") == CONSENT_WAV_URLS["en"]


def test_consent_wav_is_case_and_whitespace_tolerant():
    assert get_consent_wav_url(" HI ") == CONSENT_WAV_URLS["hi"]
    assert get_consent_wav_url("MR") == CONSENT_WAV_URLS["mr"]


# ---------------------------------------------------------------------------
# DNC check (direct)
# ---------------------------------------------------------------------------


async def test_check_dnc_returns_false_when_empty(db_session):
    assert await check_dnc(db_session, "+919999999999") is False


async def test_check_dnc_returns_true_when_entry_exists(db_session):
    await _add_to_dnc(db_session, "+919999999999")
    assert await check_dnc(db_session, "+919999999999") is True


async def test_assert_not_on_dnc_raises_on_hit(db_session):
    await _add_to_dnc(db_session, "+919999999999", DncSource.TRAI_NOTICE)
    with pytest.raises(PhoneOnDoNotCallList) as exc:
        await assert_not_on_dnc(db_session, "+919999999999", party="customer")
    # Stable code for the app's translation layer.
    assert exc.value.code == "PHONE_ON_DNC_LIST"
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# DNC integration with allocate_mapping
# ---------------------------------------------------------------------------


async def test_allocate_refuses_when_customer_phone_on_dnc(db_session):
    await _seed_number(db_session)
    consumer, _provider, booking = await _booking_pair(db_session)
    # The allocator E.164-normalises before looking up; mirror that.
    await _add_to_dnc(db_session, _ensure_e164(consumer.phone))

    with pytest.raises(PhoneOnDoNotCallList):
        await CallMaskingService(db_session).allocate_mapping(booking.id)

    # The refused path must NOT have inserted a mapping row.
    rows = (
        await db_session.execute(select(CallMapping).where(CallMapping.booking_id == booking.id))
    ).all()
    assert rows == []


async def test_allocate_refuses_when_sp_phone_on_dnc(db_session):
    await _seed_number(db_session)
    _consumer, provider, booking = await _booking_pair(db_session)
    await _add_to_dnc(db_session, _ensure_e164(provider.phone), DncSource.ADMIN)

    with pytest.raises(PhoneOnDoNotCallList):
        await CallMaskingService(db_session).allocate_mapping(booking.id)


async def test_allocate_dnc_block_does_not_burn_a_pool_number(db_session):
    """DNC check must sit in front of the pool pick.

    If DNC fired after the pool pick, a refused booking would briefly
    hold a lock on the only number — a blast of DNC-blocked confirms
    could spuriously starve the pool from the next legitimate booking's
    POV. Verify the pool is untouched after a DNC refusal.
    """
    number = await _seed_number(db_session)
    consumer, _provider, booking = await _booking_pair(db_session)
    await _add_to_dnc(db_session, _ensure_e164(consumer.phone))

    with pytest.raises(PhoneOnDoNotCallList):
        await CallMaskingService(db_session).allocate_mapping(booking.id)

    # Same number is still in the pool and still ACTIVE.
    refreshed = (
        await db_session.execute(select(VirtualNumberPool).where(VirtualNumberPool.id == number.id))
    ).scalar_one()
    assert refreshed.status == VirtualNumberStatus.ACTIVE
    # No mapping row consumed the number either.
    mapping_count = (
        await db_session.execute(
            select(CallMapping).where(CallMapping.virtual_number_id == number.id)
        )
    ).all()
    assert mapping_count == []


async def test_allocate_succeeds_when_dnc_is_empty(db_session):
    """Sanity: with no DNC entries the splice is invisible."""
    await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    mapping = await CallMaskingService(db_session).allocate_mapping(booking.id)
    assert mapping is not None
    assert mapping.booking_id == booking.id


# ---------------------------------------------------------------------------
# Recording purge
# ---------------------------------------------------------------------------


class _RecordingClientSpy:
    """Mock Exotel client. Records every delete call and can raise on cue."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail_on = fail_on or set()

    async def delete_recording(self, recording_url: str) -> None:
        self.calls.append(recording_url)
        if recording_url in self._fail_on:
            raise RuntimeError(f"simulated exotel failure for {recording_url}")


async def _seed_call_record(
    db,
    *,
    recording_url: str | None,
    recording_expires_at: datetime | None,
    started_at: datetime | None = None,
    status: CallStatus = CallStatus.COMPLETED,
) -> CallRecord:
    row = CallRecord(
        id=uuid.uuid4(),
        provider_call_sid=f"CA-{uuid.uuid4().hex[:16]}",
        from_e164="+911111111111",
        to_e164=_unique_e164(),
        bridged_to_e164="+912222222222",
        direction=CallDirection.CUSTOMER_TO_SP,
        status=status,
        duration_sec=42,
        recording_url=recording_url,
        recording_expires_at=recording_expires_at,
        started_at=started_at or (datetime.now(UTC) - timedelta(days=100)),
        ended_at=None,
    )
    db.add(row)
    await db.flush()
    return row


async def test_purge_clears_expired_recording_urls(db_session):
    now = datetime.now(UTC)
    url = "https://exotel.example/recordings/CA_SID_1.mp3"
    row = await _seed_call_record(
        db_session,
        recording_url=url,
        recording_expires_at=now - timedelta(days=1),
    )
    client = _RecordingClientSpy()

    cleared = await purge_expired_recordings_async(db_session, client=client, now=now)

    assert cleared == 1
    assert client.calls == [url]
    # Row still exists; only recording_url is nulled.
    refreshed = (
        await db_session.execute(select(CallRecord).where(CallRecord.id == row.id))
    ).scalar_one()
    assert refreshed is not None
    assert refreshed.recording_url is None


async def test_purge_leaves_future_expiry_alone(db_session):
    now = datetime.now(UTC)
    url = "https://exotel.example/recordings/CA_SID_FUTURE.mp3"
    row = await _seed_call_record(
        db_session,
        recording_url=url,
        recording_expires_at=now + timedelta(days=30),
    )
    client = _RecordingClientSpy()

    cleared = await purge_expired_recordings_async(db_session, client=client, now=now)

    assert cleared == 0
    assert client.calls == []
    refreshed = (
        await db_session.execute(select(CallRecord).where(CallRecord.id == row.id))
    ).scalar_one()
    assert refreshed.recording_url == url


async def test_purge_ignores_rows_with_no_recording_url(db_session):
    now = datetime.now(UTC)
    # Row with expired-ish timestamp but recording_url is None — already purged.
    row = await _seed_call_record(
        db_session,
        recording_url=None,
        recording_expires_at=now - timedelta(days=1),
    )
    client = _RecordingClientSpy()

    cleared = await purge_expired_recordings_async(db_session, client=client, now=now)

    assert cleared == 0
    assert client.calls == []
    # Row is still there.
    refreshed = (
        await db_session.execute(select(CallRecord).where(CallRecord.id == row.id))
    ).scalar_one()
    assert refreshed.id == row.id


async def test_purge_keeps_url_when_exotel_delete_fails(db_session):
    """A failed Exotel delete must NOT clear recording_url.

    Otherwise we lose the pointer to a recording we never actually
    deleted, and the next sweep has nothing to retry.
    """
    now = datetime.now(UTC)
    ok_url = "https://exotel.example/recordings/CA_OK.mp3"
    fail_url = "https://exotel.example/recordings/CA_FAIL.mp3"
    ok_row = await _seed_call_record(
        db_session,
        recording_url=ok_url,
        recording_expires_at=now - timedelta(days=2),
    )
    fail_row = await _seed_call_record(
        db_session,
        recording_url=fail_url,
        recording_expires_at=now - timedelta(days=2),
    )
    client = _RecordingClientSpy(fail_on={fail_url})

    cleared = await purge_expired_recordings_async(db_session, client=client, now=now)

    assert cleared == 1
    # Both URLs got a delete attempt.
    assert set(client.calls) == {ok_url, fail_url}
    # OK row nulled.
    ok_refreshed = (
        await db_session.execute(select(CallRecord).where(CallRecord.id == ok_row.id))
    ).scalar_one()
    assert ok_refreshed.recording_url is None
    # Failed row still carries the URL — next sweep retries.
    fail_refreshed = (
        await db_session.execute(select(CallRecord).where(CallRecord.id == fail_row.id))
    ).scalar_one()
    assert fail_refreshed.recording_url == fail_url


async def test_purge_sweep_on_empty_set_is_noop(db_session):
    now = datetime.now(UTC)
    client = _RecordingClientSpy()
    cleared = await purge_expired_recordings_async(db_session, client=client, now=now)
    assert cleared == 0
    assert client.calls == []


# ---------------------------------------------------------------------------
# Exotel recording-SID parser (pure unit)
# ---------------------------------------------------------------------------


def test_extract_recording_sid_parses_exotel_url():
    from app.services.consent import _extract_recording_sid

    sid = _extract_recording_sid(
        "https://api.exotel.com/v1/Accounts/abc/Recordings/CA12345.mp3?sig=xyz"
    )
    assert sid == "CA12345"


def test_extract_recording_sid_handles_no_extension():
    from app.services.consent import _extract_recording_sid

    sid = _extract_recording_sid("https://api.exotel.com/v1/Accounts/abc/Recordings/CA12345")
    assert sid == "CA12345"


def test_extract_recording_sid_raises_on_garbage():
    from app.services.consent import _extract_recording_sid

    with pytest.raises(ValueError):
        _extract_recording_sid("")


# Silence an unused-import warning on Any imported for typing hints we didn't end up using.
_ = Any
