"""
SVC-324: a booking names who is doing it and for which business.

``professional_id`` + ``professional`` (the assigned Pro) and ``service_provider``
(the SP's brand) on ``BookingResponse``, for the consumer (Phase C) and, widened
on 2026-10-07, for the SP: the SP's job screen could not show who was assigned,
or that a team Pro had declined and the job was waiting for a re-pick.
"""

from unittest.mock import patch

import pytest

from app.models.booking import BookingStatus
from app.models.user import User
from app.services.provider_service import ProviderService
from app.tasks import notification_tasks
from tests.test_accept_contract import _booking, _service
from tests.test_two_level_dispatch import (
    DECLINE,
    _as,
    _assigned_to,
    _team_sp,
)

BOOKING = "/api/v1/bookings/{}"
BOOKINGS = "/api/v1/bookings/"


@pytest.fixture
def pushes():
    """Swallow the pushes the routes enqueue; this file doesn't assert on them."""
    with patch.object(notification_tasks.send_booking_notification, "delay"):
        yield


async def _brand(db, sp, name="Sparkle Cleaners"):
    profile = await ProviderService(db).get_profile(sp.id)
    profile.business_name = name
    await db.flush()


def _row(listing: dict, booking_id) -> dict:
    [row] = [b for b in listing["bookings"] if b["id"] == str(booking_id)]
    return row


@pytest.mark.asyncio
async def test_an_unassigned_booking_names_nobody(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    consumer, booking = await _booking(db_session, service=service)

    body = (
        await client.get(
            BOOKING.format(booking.id), headers=_as(auth_headers, consumer, role="consumer")
        )
    ).json()

    assert body["professional_id"] is None
    assert body["professional"] is None
    assert body["service_provider"] is None


@pytest.mark.asyncio
async def test_both_parties_see_the_assigned_pro_and_the_business(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    await _brand(db_session, sp)
    consumer, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    for viewer, role in ((consumer, "consumer"), (sp, "provider")):
        headers = _as(auth_headers, viewer, role=role)
        detail = (await client.get(BOOKING.format(booking.id), headers=headers)).json()
        listed = _row((await client.get(BOOKINGS, headers=headers)).json(), booking.id)
        for body in (detail, listed):
            assert body["professional_id"] == str(member.id)
            assert body["professional"] == {
                "id": str(member.id),
                "display_name": "Rohan",
                "profile_photo_url": None,
                "is_self": False,
                "rating_average": None,
                "rating_count": 0,
                "bookings_completed_count": 0,
                "is_background_verified": True,
            }
            assert body["service_provider"] == {
                "user_id": str(sp.id),
                "business_name": "Sparkle Cleaners",
                "business_image_url": None,
            }


@pytest.mark.asyncio
async def test_the_brief_carries_no_phone_or_payout_details(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    consumer, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    body = (
        await client.get(
            BOOKING.format(booking.id), headers=_as(auth_headers, consumer, role="consumer")
        )
    ).json()

    for key in ("phone", "user_id", "payout_vpa", "payout_account_number", "kyc_status"):
        assert key not in body["professional"]
    assert not any("payout" in key for key in body["service_provider"])


@pytest.mark.asyncio
async def test_after_a_decline_the_sp_sees_a_job_waiting_for_a_repick(
    client, db_session, auth_headers, pushes
):
    """The state the SP-side widening exists for: ACCEPTED, theirs, nobody on it."""
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    member_user = await db_session.get(User, member.user_id)
    resp = await client.post(
        DECLINE.format(booking.id), headers=_as(auth_headers, member_user, role="consumer")
    )
    assert resp.json()["outcome"] == "awaiting_repick"

    body = (await client.get(BOOKING.format(booking.id), headers=_as(auth_headers, sp))).json()
    assert body["status"] == BookingStatus.ACCEPTED.value
    assert body["provider_id"] == str(sp.id)
    assert body["professional_id"] is None
    assert body["professional"] is None
    assert body["service_provider"]["user_id"] == str(sp.id), "still the SP's job"


@pytest.mark.asyncio
async def test_a_solo_job_is_marked_as_the_sps_own(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, self_pro, _ = await _team_sp(db_session, service)
    consumer, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, self_pro)

    body = (
        await client.get(
            BOOKING.format(booking.id), headers=_as(auth_headers, consumer, role="consumer")
        )
    ).json()

    assert body["professional"]["id"] == str(self_pro.id)
    assert body["professional"]["is_self"] is True
