"""provider_phone reaches the consumer, and only while the job is live (SVC-360).

The tracking screen's Call button dialled a placeholder because nothing in the
consumer's view carried the Pro's number. ``BookingResponse.provider_phone`` is
the interim direct dial until masked calling lands: the assigned Pro's own login
phone (the owner's, for a Pro-for-self; never the SP's for a team Pro).

It is gated three ways, and each test below pins one edge of that gate:

* the viewer is the consumer, so the SP, the assigned Pro (rendered as the SP by
  ``viewer_for``) and admin get ``None``;
* a Pro is assigned, so an ACCEPTED booking whose Pro declined (``professional_id``
  NULL while the SP re-picks) gets ``None``;
* the status is accepted / en_route / in_progress, so a job not yet taken or
  already over gets ``None``.

Both booking GETs are covered, since the list route serialises a whole page.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.booking import BookingStatus
from app.models.user import User
from app.schemas.booking import BookingResponse
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _service
from tests.test_two_level_dispatch import _team_sp

LIVE = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)
NOT_LIVE = (
    BookingStatus.REQUESTED,
    BookingStatus.COMPLETED,
    BookingStatus.CANCELLED,
    BookingStatus.EXPIRED,
)


def _as(auth_headers, user, role):
    return auth_headers(str(user.id), role)


async def _job(db, status, *, team=False):
    """A booking assigned to an SP's Pro-for-self, or (``team``) to a team Pro.

    Returns the consumer, the SP, the assigned Pro and the Pro's user.
    """
    service = await _service(db)
    sp, self_pro, member = await _team_sp(db, service)
    pro = member if team else self_pro
    consumer, booking = await _booking(db, service=service, provider_id=sp.id, status=status)
    booking.professional_id = pro.id
    await db.flush()
    pro_user = await db.get(User, pro.user_id)
    return consumer, sp, pro, pro_user, booking


async def _detail(client, headers, booking):
    resp = await client.get(f"/api/v1/bookings/{booking.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _row(client, headers, booking, url="/api/v1/bookings/"):
    resp = await client.get(url, headers=headers)
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json()["bookings"] if r["id"] == str(booking.id)]
    assert len(rows) == 1, rows
    return rows[0]


# --- (a) the consumer gets it while the job is live ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LIVE)
async def test_consumer_gets_the_pros_phone_while_live(client, auth_headers, db_session, status):
    consumer, sp, _, _, booking = await _job(db_session, status)
    headers = _as(auth_headers, consumer, "consumer")

    # Pro-for-self: the Pro's login phone is the owner's.
    assert (await _detail(client, headers, booking))["provider_phone"] == sp.phone
    assert (await _row(client, headers, booking))["provider_phone"] == sp.phone


@pytest.mark.asyncio
async def test_team_pro_gives_the_pros_own_phone_not_the_sps(client, auth_headers, db_session):
    consumer, sp, _, pro_user, booking = await _job(db_session, BookingStatus.EN_ROUTE, team=True)
    headers = _as(auth_headers, consumer, "consumer")

    assert pro_user.phone != sp.phone
    assert (await _detail(client, headers, booking))["provider_phone"] == pro_user.phone
    assert (await _row(client, headers, booking))["provider_phone"] == pro_user.phone


@pytest.mark.asyncio
async def test_hydrator_loads_the_pros_user_on_a_cold_session(client, auth_headers, db_session):
    """``attach_professionals`` must load each Pro's user itself.

    The other tests can't show it: the fixtures leave every ``User`` in the
    shared session, and a lazy many-to-one is answered from there with no SQL.
    Emptying the session is what a production request starts with, so without
    the hydrator's fetch, reading ``.user`` here is a MissingGreenlet.
    """
    consumer, _, _, pro_user, booking = await _job(db_session, BookingStatus.EN_ROUTE, team=True)
    headers = _as(auth_headers, consumer, "consumer")
    phone, booking_id = pro_user.phone, booking.id
    db_session.expunge_all()

    resp = await client.get(f"/api/v1/bookings/{booking_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider_phone"] == phone


# --- (b) not before the job is taken, nor after it is over -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NOT_LIVE)
async def test_consumer_gets_none_outside_the_live_window(client, auth_headers, db_session, status):
    consumer, _, _, _, booking = await _job(db_session, status)
    headers = _as(auth_headers, consumer, "consumer")

    assert (await _detail(client, headers, booking))["provider_phone"] is None
    assert (await _row(client, headers, booking))["provider_phone"] is None


# --- (c) not while the SP re-picks after a decline -------------------------------


@pytest.mark.asyncio
async def test_accepted_with_no_pro_gives_none(client, auth_headers, db_session):
    """A team Pro's decline leaves the booking ACCEPTED with ``professional_id``
    NULL until the SP re-picks. The Call button must go with the Pro."""
    consumer, _, _, _, booking = await _job(db_session, BookingStatus.ACCEPTED, team=True)
    booking.professional_id = None
    await db_session.flush()
    headers = _as(auth_headers, consumer, "consumer")

    assert (await _detail(client, headers, booking))["provider_phone"] is None
    assert (await _row(client, headers, booking))["provider_phone"] is None


# --- (d) nobody but the consumer gets it -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LIVE)
async def test_sp_gets_none(client, auth_headers, db_session, status):
    _, sp, _, _, booking = await _job(db_session, status, team=True)
    headers = _as(auth_headers, sp, "provider")

    assert (await _detail(client, headers, booking))["provider_phone"] is None
    assert (await _row(client, headers, booking))["provider_phone"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LIVE)
async def test_assigned_pro_gets_none(client, auth_headers, db_session, status):
    _, _, _, pro_user, booking = await _job(db_session, status, team=True)
    # Team Pros sign up as consumers (SVC-320).
    headers = _as(auth_headers, pro_user, "consumer")

    assert (await _detail(client, headers, booking))["provider_phone"] is None
    assert (await _row(client, headers, booking, "/api/v1/pros/me/jobs"))["provider_phone"] is None


@pytest.mark.asyncio
async def test_admin_gets_none_on_list_and_detail(client, auth_headers, db_session):
    _, _, _, _, booking = await _job(db_session, BookingStatus.EN_ROUTE)
    admin = await UserFactory.create(db_session, role="admin")
    headers = _as(auth_headers, admin, "admin")

    resp = await client.get(f"/api/v1/admin/bookings/{booking.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider_phone"] is None
    assert (await _row(client, headers, booking, "/api/v1/admin/bookings"))[
        "provider_phone"
    ] is None


# --- the gate reads a normalised status ------------------------------------------


def _bare_booking(status):
    """Just enough of a booking for ``for_viewer``, with ``status`` as given."""
    consumer_id = uuid.uuid4()
    pro = SimpleNamespace(
        id=uuid.uuid4(),
        display_name="Rohan",
        profile_photo_url=None,
        is_self=False,
        user=SimpleNamespace(phone="+919800000001"),
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        consumer_id=consumer_id,
        provider_id=uuid.uuid4(),
        items=[],
        status=status,
        service_amount=100.0,
        platform_commission=20.0,
        provider_payout=80.0,
        discount_amount=0.0,
        tip_amount=0.0,
        payment_method="online",
        payment_status="pending",
        doorstep_otp="4242",
        consumer_address=None,
        status_logs=[],
        professional_id=pro.id,
        professional=pro,
        scheduled_at=None,
        created_at=datetime(2026, 10, 15, tzinfo=UTC),
    )


@pytest.mark.parametrize("status", [BookingStatus.EN_ROUTE, "en_route"])
def test_gate_accepts_enum_or_plain_string_status(status):
    booking = _bare_booking(status)
    response = BookingResponse.for_viewer(booking, booking.consumer_id)
    assert response.provider_phone == "+919800000001"


@pytest.mark.parametrize("status", [BookingStatus.COMPLETED, "completed"])
def test_gate_refuses_enum_or_plain_string_status(status):
    booking = _bare_booking(status)
    assert BookingResponse.for_viewer(booking, booking.consumer_id).provider_phone is None


def test_non_consumer_view_never_reads_the_pros_user():
    """The phone is read only after the gate passes, so a path that skipped
    loading ``Professional.user`` can't fail for anyone but the consumer."""

    class Unloaded:
        @property
        def phone(self):
            raise AssertionError("read Professional.user for a non-consumer")

    booking = _bare_booking(BookingStatus.EN_ROUTE)
    booking.professional.user = Unloaded()
    assert BookingResponse.for_viewer(booking, booking.provider_id).provider_phone is None
    assert BookingResponse.for_viewer(booking, None).provider_phone is None
