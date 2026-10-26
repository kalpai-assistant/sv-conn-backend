"""
SVC-396: the assigned Pro's trust fields on ``BookingResponse.professional``.

``rating_average`` / ``rating_count`` (reviews whose ``professional_id`` is the
Pro), ``bookings_completed_count`` and ``is_background_verified``, for the
consumer app's ProTrustCard (SVC-386). They are the Pro's own record, not the
SP's, so a team Pro and the SP's Pro-for-self are counted apart. Also pins the
write that makes the rating mean anything: a new review now carries the
booking's Pro.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import event

from app.models.booking import BookingStatus
from app.models.provider_profile import KYCStatus
from app.models.review import Review
from app.models.user import User
from app.services.account_service import AccountService
from app.services.booking_service import BookingService
from app.services.provider_service import ProviderService
from app.tasks import notification_tasks
from tests.test_accept_contract import _booking, _service
from tests.test_two_level_dispatch import _as, _team_pro, _team_sp

BOOKING = "/api/v1/bookings/{}"
REVIEWS = "/api/v1/reviews/"


@pytest.fixture
def pushes():
    """Swallow the pushes the routes enqueue; this file doesn't assert on them."""
    with patch.object(notification_tasks.send_booking_notification, "delay"):
        yield


async def _job(db, service, sp, pro, status=BookingStatus.COMPLETED):
    """A booking of ``sp``'s that ``pro`` did (or is doing)."""
    consumer, booking = await _booking(db, service=service, provider_id=sp.id, status=status)
    booking.professional_id = pro.id
    await db.flush()
    return consumer, booking


async def _review(db, booking, rating):
    db.add(
        Review(
            booking_id=booking.id,
            reviewer_id=booking.consumer_id,
            reviewee_id=booking.provider_id,
            professional_id=booking.professional_id,
            rating=rating,
        )
    )
    await db.flush()


async def _brief(client, auth_headers, consumer, booking) -> dict:
    resp = await client.get(
        BOOKING.format(booking.id), headers=_as(auth_headers, consumer, role="consumer")
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["professional"]


@pytest.mark.asyncio
async def test_a_new_review_is_written_against_the_bookings_pro(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    consumer, booking = await _job(db_session, service, sp, member)

    resp = await client.post(
        REVIEWS,
        json={"booking_id": str(booking.id), "rating": 4},
        headers=_as(auth_headers, consumer, role="consumer"),
    )

    assert resp.status_code == 200, resp.text
    review = await db_session.get(Review, resp.json()["id"])
    assert review.professional_id == member.id
    assert review.reviewee_id == sp.id, "the SP is still the reviewee"


@pytest.mark.asyncio
async def test_the_pro_carries_their_own_rating_and_completed_jobs(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    consumer, shown = await _job(db_session, service, sp, member)
    await _review(db_session, shown, 5)
    _, other = await _job(db_session, service, sp, member)
    await _review(db_session, other, 4)
    await _job(db_session, service, sp, member)  # completed, never reviewed
    await _job(db_session, service, sp, member, status=BookingStatus.CANCELLED)
    # The same SP's other Pro: none of this is the member's.
    _, owners = await _job(db_session, service, sp, self_pro)
    await _review(db_session, owners, 1)

    brief = await _brief(client, auth_headers, consumer, shown)

    assert brief["id"] == str(member.id)
    assert brief["rating_average"] == 4.5
    assert brief["rating_count"] == 2
    assert brief["bookings_completed_count"] == 3
    assert brief["is_background_verified"] is True


@pytest.mark.asyncio
async def test_a_pro_with_no_reviews_has_no_average(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    consumer, booking = await _job(db_session, service, sp, member, status=BookingStatus.ACCEPTED)

    brief = await _brief(client, auth_headers, consumer, booking)

    assert brief["rating_average"] is None
    assert brief["rating_count"] == 0
    assert brief["bookings_completed_count"] == 0


@pytest.mark.asyncio
async def test_the_average_is_rounded_to_one_place(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    consumer, shown = await _job(db_session, service, sp, member)
    await _review(db_session, shown, 5)
    for _ in range(2):
        _, booking = await _job(db_session, service, sp, member)
        await _review(db_session, booking, 4)

    brief = await _brief(client, auth_headers, consumer, shown)

    assert brief["rating_average"] == 4.3  # 13 / 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kyc", [KYCStatus.PENDING, KYCStatus.SUBMITTED, KYCStatus.REJECTED])
async def test_an_unverified_team_pro_is_not_background_verified(
    client, db_session, auth_headers, pushes, kyc
):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    pro = await _team_pro(db_session, sp.id, name="Amit", kyc=kyc)
    consumer, booking = await _job(db_session, service, sp, pro, status=BookingStatus.ACCEPTED)

    brief = await _brief(client, auth_headers, consumer, booking)

    assert brief["is_background_verified"] is False


@pytest.mark.asyncio
async def test_a_pro_for_self_is_verified_by_its_sps_kyc(client, db_session, auth_headers, pushes):
    """The self row's own ``kyc_status`` is never synced (SVC-329): it stays
    what it was created with, and only the SP's counts."""
    service = await _service(db_session)
    sp, self_pro, _ = await _team_sp(db_session, service)
    self_pro.kyc_status = KYCStatus.PENDING
    await db_session.flush()
    consumer, booking = await _job(db_session, service, sp, self_pro, status=BookingStatus.ACCEPTED)

    assert (await _brief(client, auth_headers, consumer, booking))["is_background_verified"]

    profile = await ProviderService(db_session).get_profile(sp.id)
    profile.kyc_status = KYCStatus.REJECTED
    self_pro.kyc_status = KYCStatus.VERIFIED
    await db_session.flush()

    assert not (await _brief(client, auth_headers, consumer, booking))["is_background_verified"]


@pytest.mark.asyncio
async def test_a_deleted_pro_keeps_their_record(client, db_session, auth_headers, pushes):
    """SVC-367 tombstones a Pro rather than deleting them; past bookings still
    render, and the counts still hang off the same row."""
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    consumer, booking = await _job(db_session, service, sp, member)
    await _review(db_session, booking, 5)

    await AccountService(db_session).delete_account(member.user_id)
    await db_session.flush()

    brief = await _brief(client, auth_headers, consumer, booking)
    assert brief["display_name"] == "Deleted professional"
    assert brief["rating_count"] == 1
    assert brief["bookings_completed_count"] == 1
    assert (await db_session.get(User, member.user_id)).is_active is False


@pytest.mark.asyncio
async def test_a_page_of_bookings_costs_a_fixed_number_of_queries(db_session):
    """The trust fields come from one grouped query, however many Pros are on the page."""
    service = await _service(db_session)

    async def statements_for(n_pros):
        bookings = []
        for _ in range(n_pros):
            sp, self_pro, _ = await _team_sp(db_session, service)
            _, booking = await _job(db_session, service, sp, self_pro)
            await _review(db_session, booking, 5)
            bookings.append(booking)
        seen = []
        engine = db_session.bind.sync_engine

        def count(*_):
            seen.append(1)

        event.listen(engine, "before_cursor_execute", count)
        try:
            await BookingService(db_session).attach_professionals(bookings)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert all(b.professional.rating_count == 1 for b in bookings)
        return len(seen)

    assert await statements_for(1) == await statements_for(5) == 4
