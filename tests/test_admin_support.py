"""SVC-433: the support console API.

Lookup (each type, and an input that fits two), the customer profile, append-only
notes, retry dispatch (incl. zero recipients, the window restart through the real
sweep, the double click) and reassign-Pro within the SP (assign()'s refusals, the
pushes, the list matching the POST). Every write has an audit row; every route
answers admin, ops and support alike.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.database import _run_after_commit
from app.models.address import Address
from app.models.admin_action import AdminAction
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.booking_assignment_event import BookingAssignmentEvent
from app.models.booking_dispute import BookingDispute
from app.models.booking_offer import BookingOffer, OfferResponse
from app.models.booking_support_note import BookingSupportNote
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import AdminRole
from app.services.admin_support_service import interpret, phone_digits
from app.tasks import notification_tasks
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_admin_console_auth import _admin, _session
from tests.test_paid_no_taker import (  # noqa: F401  (fixture)
    _get,
    _seed,
    _sweep,
    committed_sweep_world,
)
from tests.test_two_level_dispatch import _team_pro, _team_sp

API = "/api/v1/admin"
LOOKUP = f"{API}/support/lookup"
CUSTOMER = API + "/support/customers/{}"
NOTES = API + "/bookings/{}/notes"
CONTACTED = API + "/bookings/{}/contacted"
RETRY = API + "/bookings/{}/retry-dispatch"
ELIGIBLE = API + "/bookings/{}/eligible-pros"
REASSIGN = API + "/bookings/{}/reassign-pro"


@pytest.fixture
async def admins(db_session):
    """Made first: ``_admin`` commits, and nothing else in a test should be."""
    return SimpleNamespace(
        admin=await _admin(db_session, AdminRole.ADMIN),
        ops=await _admin(db_session, AdminRole.OPS),
        support=await _admin(db_session, AdminRole.SUPPORT),
    )


@pytest.fixture
def pushes():
    sent: list[tuple[str, dict]] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append((kw["event"], kw)),
    ):
        yield sent


async def _profile(db, user_id) -> ProviderProfile:
    return await db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == user_id))


def _phone() -> str:
    return f"+919{uuid.uuid4().int % 10**9:09d}"


async def _audit(db, action: str, booking_id) -> list[AdminAction]:
    return list(
        await db.scalars(
            select(AdminAction).where(
                AdminAction.action == action, AdminAction.target_id == str(booking_id)
            )
        )
    )


# --- Lookup: parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "digits"),
    [
        ("9876543210", "9876543210"),
        ("98765 43210", "9876543210"),
        ("+91 98765-43210", "9876543210"),
        ("919876543210", "9876543210"),
        ("09876543210", "9876543210"),
        ("(+91) 98765.43210", "9876543210"),
        ("987654321", None),
        ("98765x43210", None),
    ],
)
def test_phone_is_tolerant_of_formatting(raw, digits):
    assert phone_digits(raw) == digits


def test_interpret_lists_every_type_an_input_fits():
    assert list(interpret("facade")) == ["booking_id_suffix", "name"]
    assert list(interpret("987650")) == ["booking_id_suffix"]
    assert list(interpret("ramesh plumb")) == ["name"]
    assert list(interpret("98765 43210")) == ["phone"]
    full = str(uuid.uuid4())
    assert list(interpret(full)) == ["booking_id"]
    assert list(interpret(full.replace("-", ""))) == ["booking_id"]


# --- Lookup: the route -------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_by_phone_finds_the_customer_the_sp_and_the_pro(client, db_session, admins):
    phone = _phone()
    customer = await UserFactory.create(db_session, role="consumer", phone=phone, name="Asha")
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    sp.phone = _phone()
    await db_session.flush()
    # A team Pro who signed up as a consumer with the same number: the same
    # person, so listed as a customer and as a Pro.
    pro = await _team_pro(db_session, sp.id, name="Rohan")
    pro_user_phone = _phone()
    from app.models.user import User

    (await db_session.get(User, pro.user_id)).phone = pro_user_phone
    await db_session.flush()

    spaced = f"{phone[3:8]} {phone[8:]}"
    resp = await client.get(LOOKUP, params={"q": spaced}, headers=_session(admins.support))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["interpreted_as"] == ["phone"]
    assert [c["user_id"] for c in body["customers"]] == [str(customer.id)]
    assert body["providers"] == [] and body["pros"] == [] and body["bookings"] == []

    resp = await client.get(
        LOOKUP, params={"q": "+91-" + sp.phone[3:]}, headers=_session(admins.support)
    )
    assert [p["user_id"] for p in resp.json()["providers"]] == [str(sp.id)]
    assert resp.json()["providers"][0]["matched_by"] == "phone"

    resp = await client.get(
        LOOKUP, params={"q": "0" + pro_user_phone[3:]}, headers=_session(admins.support)
    )
    body = resp.json()
    assert [p["professional_id"] for p in body["pros"]] == [str(pro.id)]
    assert body["pros"][0]["service_provider"]["user_id"] == str(sp.id)
    assert [c["user_id"] for c in body["customers"]] == [str(pro.user_id)]


@pytest.mark.asyncio
async def test_lookup_by_full_booking_id_and_by_its_last_six(client, db_session, admins):
    consumer, booking = await _booking(db_session)
    headers = _session(admins.ops)

    for q in (str(booking.id), str(booking.id).replace("-", "").upper()):
        resp = await client.get(LOOKUP, params={"q": q}, headers=headers)
        assert resp.status_code == 200, resp.text
        rows = resp.json()["bookings"]
        assert [(b["id"], b["matched_by"]) for b in rows] == [(str(booking.id), "id")]
        assert rows[0]["consumer_phone"] == consumer.phone
        assert rows[0]["service_summary"] == "Deep Cleaning"

    resp = await client.get(LOOKUP, params={"q": str(booking.id)[-6:]}, headers=headers)
    rows = resp.json()["bookings"]
    assert str(booking.id) in {b["id"] for b in rows}
    assert {b["matched_by"] for b in rows} == {"id_suffix"}


@pytest.mark.asyncio
async def test_an_input_that_fits_two_types_returns_both_typed(client, db_session, admins):
    """ "facade" is valid hex and a word: never guess, search both."""
    new_id = uuid.UUID(f"{uuid.uuid4().hex[:26]}facade")
    real_uuid4 = uuid.uuid4
    ids = iter([new_id])
    # The factory's first uuid4() is the booking id.
    consumer = await UserFactory.create(db_session, role="consumer")
    service_row = await _service(db_session)
    address = Address(user_id=consumer.id, label="home", full_address="1 A Road")
    db_session.add(address)
    await db_session.flush()
    with patch("uuid.uuid4", lambda: next(ids, None) or real_uuid4()):
        await BookingFactory.create(
            db_session,
            consumer_id=consumer.id,
            provider_id=None,
            service_id=service_row.id,
            address_id=address.id,
        )
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    (await _profile(db_session, sp.id)).business_name = "Facade Painters"
    await db_session.flush()

    resp = await client.get(LOOKUP, params={"q": "facade"}, headers=_session(admins.admin))
    body = resp.json()
    assert body["interpreted_as"] == ["booking_id_suffix", "name"]
    assert str(new_id) in {b["id"] for b in body["bookings"]}
    assert str(sp.id) in {p["user_id"] for p in body["providers"]}
    assert all(p["matched_by"] == "name" for p in body["providers"])


@pytest.mark.asyncio
async def test_sp_name_search_needs_every_word(client, db_session, admins):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    sp.name = f"Ramesh{uuid.uuid4().hex[:6]} Kumar"
    (await _profile(db_session, sp.id)).business_name = "Kumar Plumbing Works"
    await db_session.flush()
    first = sp.name.split()[0].lower()
    headers = _session(admins.support)

    resp = await client.get(LOOKUP, params={"q": f"{first} plumb"}, headers=headers)
    assert [p["user_id"] for p in resp.json()["providers"]] == [str(sp.id)]
    resp = await client.get(LOOKUP, params={"q": f"{first} carpentry"}, headers=headers)
    assert resp.json()["providers"] == []


@pytest.mark.asyncio
async def test_lookup_query_too_short_is_422(client, admins):
    resp = await client.get(LOOKUP, params={"q": "ab"}, headers=_session(admins.support))
    assert resp.status_code == 422


# --- Customer profile --------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_profile_counts_and_lifetime_spend(client, db_session, admins):
    customer = await UserFactory.create(db_session, role="consumer", name="Asha")
    live = Address(user_id=customer.id, label="home", full_address="1 A Road")
    gone = Address(
        user_id=customer.id,
        label="old",
        full_address="2 B Road",
        deleted_at=datetime.now(UTC),
    )
    db_session.add_all([live, gone])
    await db_session.flush()

    async def booking(status, amount, discount=0, tip=0, age_minutes=0):
        b = await BookingFactory.create(
            db_session,
            consumer_id=customer.id,
            provider_id=None,
            address_id=live.id,
            status=status,
            service_amount=amount,
        )
        b.discount_amount = Decimal(discount)
        b.tip_amount = Decimal(tip)
        b.created_at = datetime.now(UTC) - timedelta(minutes=age_minutes)
        await db_session.flush()
        return b

    done = await booking(BookingStatus.COMPLETED, 1000, discount=100, tip=50, age_minutes=60)
    refunded = await booking(BookingStatus.COMPLETED, 600, age_minutes=50)
    await booking(BookingStatus.CANCELLED, 900, age_minutes=40)
    for minutes in range(10):
        await booking(BookingStatus.EXPIRED, 300, age_minutes=minutes)
    db_session.add_all(
        [
            # Went through: counts against spend.
            BookingDispute(
                booking_id=refunded.id,
                raised_by_user_id=customer.id,
                reason_code="QUALITY",
                description="x",
                status="CLOSED",
                refund_amount=Decimal("200"),
                refund_channel="RAZORPAY",
                refund_status="PROCESSED",
            ),
            # Still pending at Razorpay: doesn't.
            BookingDispute(
                booking_id=done.id,
                raised_by_user_id=customer.id,
                reason_code="QUALITY",
                description="x",
                status="RESOLVED",
                refund_amount=Decimal("75"),
                refund_channel="RAZORPAY",
                refund_status="PENDING",
            ),
        ]
    )
    await db_session.flush()

    resp = await client.get(CUSTOMER.format(customer.id), headers=_session(admins.support))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["address_count"] == 1
    assert body["lifetime_bookings"] == 13
    assert body["completed_bookings"] == 2
    # (1000 − 100 + 50) + 600 − 200
    assert body["lifetime_spend"] == 1350.0
    recent = body["recent_bookings"]
    assert len(recent) == 10
    assert [r["created_at"] for r in recent] == sorted(
        (r["created_at"] for r in recent), reverse=True
    )
    assert str(done.id) not in {r["id"] for r in recent}

    # "View all" for this customer, even once their phone is a placeholder.
    resp = await client.get(
        f"{API}/bookings",
        params={"consumer_id": str(customer.id), "per_page": 50},
        headers=_session(admins.support),
    )
    assert resp.json()["total"] == 13


@pytest.mark.asyncio
async def test_customer_profile_404s(client, db_session, admins):
    resp = await client.get(CUSTOMER.format(uuid.uuid4()), headers=_session(admins.support))
    assert resp.status_code == 404
    resp = await client.get(CUSTOMER.format(admins.admin.id), headers=_session(admins.support))
    assert resp.status_code == 404


# --- Notes -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_are_appended_with_author_and_audited_without_text(client, db_session, admins):
    _, booking = await _booking(db_session)

    resp = await client.post(
        NOTES.format(booking.id),
        json={"body": "  Customer says the cleaner was late  "},
        headers=_session(admins.support),
    )
    assert resp.status_code == 201, resp.text
    note = resp.json()
    assert note["kind"] == "note"
    assert note["body"] == "Customer says the cleaner was late"
    assert note["author"]["user_id"] == str(admins.support.id)
    assert note["author"]["email"] == admins.support.email
    assert note["author"]["admin_role"] == "support"
    assert note["created_at"]

    resp = await client.post(CONTACTED.format(booking.id), headers=_session(admins.ops))
    assert resp.status_code == 201, resp.text
    contacted = resp.json()
    assert contacted["kind"] == "contacted" and contacted["body"] is None

    resp = await client.get(NOTES.format(booking.id), headers=_session(admins.admin))
    assert [n["id"] for n in resp.json()] == [contacted["id"], note["id"]]

    (added,) = await _audit(db_session, "booking.note_add", booking.id)
    assert added.admin_user_id == admins.support.id
    assert added.details == {"note_id": note["id"], "kind": "note"}
    assert "late" not in str(added.details) + str(added.after_json)
    (marked,) = await _audit(db_session, "booking.contacted", booking.id)
    assert marked.details == {"note_id": contacted["id"], "kind": "contacted"}


@pytest.mark.asyncio
async def test_a_note_needs_text_but_contacted_doesnt(client, db_session, admins):
    _, booking = await _booking(db_session)
    headers = _session(admins.support)
    resp = await client.post(NOTES.format(booking.id), json={"body": "   "}, headers=headers)
    assert resp.status_code == 422
    assert resp.json().get("code") == "NOTE_EMPTY"
    resp = await client.post(NOTES.format(booking.id), json={}, headers=headers)
    assert resp.status_code == 422
    resp = await client.post(
        CONTACTED.format(booking.id), json={"body": "Called, no answer"}, headers=headers
    )
    assert resp.status_code == 201 and resp.json()["body"] == "Called, no answer"
    resp = await client.post(NOTES.format(uuid.uuid4()), json={"body": "x"}, headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_notes_cannot_be_edited_or_deleted(client, db_session, admins):
    _, booking = await _booking(db_session)
    headers = _session(admins.admin)
    resp = await client.post(NOTES.format(booking.id), json={"body": "first"}, headers=headers)
    note_id = resp.json()["id"]
    for method in ("PATCH", "PUT", "DELETE"):
        resp = await client.request(
            method, f"{NOTES.format(booking.id)}/{note_id}", headers=headers
        )
        assert resp.status_code in (404, 405)
        resp = await client.request(method, NOTES.format(booking.id), headers=headers)
        assert resp.status_code == 405


@pytest.mark.asyncio
async def test_the_db_refuses_a_note_without_text(db_session, admins):
    _, booking = await _booking(db_session)
    db_session.add(
        BookingSupportNote(
            booking_id=booking.id, author_user_id=admins.admin.id, kind="note", body=None
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


# --- Retry dispatch ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_dispatch_rebroadcasts_with_a_fresh_window(client, db_session, admins, pushes):
    service = await _service(db_session)
    await _eligible_provider(db_session, service)
    declined_sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    booking.payment_status = PaymentStatus.PAID
    booking.created_at = datetime.now(UTC) - timedelta(minutes=3)
    booking.last_chance_at = datetime.now(UTC) - timedelta(seconds=60)
    db_session.add(
        BookingOffer(
            booking_id=booking.id,
            provider_id=declined_sp.id,
            response=OfferResponse.DECLINED,
            responded_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    resp = await client.post(RETRY.format(booking.id), headers=_session(admins.support))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The SP who declined isn't asked again.
    assert body["recipients"] == 1
    assert body["status"] == "requested"

    await db_session.refresh(booking)
    assert booking.dispatch_retried_at is not None
    assert booking.last_chance_at is None
    assert booking.status is BookingStatus.REQUESTED
    timeout = get_settings().BOOKING_REQUEST_TIMEOUT_SECONDS
    assert booking.offer_expires_at == booking.dispatch_retried_at + timedelta(seconds=timeout)
    # The window restart doesn't pretend the booking was reopened.
    assert booking.reopened_at is None

    (row,) = await _audit(db_session, "booking.retry_dispatch", booking.id)
    assert row.admin_user_id == admins.support.id
    assert row.details == {"recipients": 1}

    # The broadcast is the existing task, and only once the change commits.
    assert pushes == []
    _run_after_commit(db_session.sync_session)
    assert [(e, kw["booking_id"]) for e, kw in pushes] == [("new_request", str(booking.id))]


@pytest.mark.asyncio
async def test_retry_dispatch_with_nobody_eligible_changes_nothing(
    client, db_session, admins, pushes
):
    service = await _service(db_session)
    await _eligible_provider(db_session, service, online=False)
    _, booking = await _booking(db_session, service=service)

    resp = await client.post(RETRY.format(booking.id), headers=_session(admins.ops))
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "NO_ELIGIBLE_PROVIDERS"
    assert resp.json()["details"]["reason"]

    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED
    assert booking.dispatch_retried_at is None
    assert await _audit(db_session, "booking.retry_dispatch", booking.id) == []
    _run_after_commit(db_session.sync_session)
    assert pushes == []


@pytest.mark.asyncio
async def test_retry_dispatch_only_for_a_requested_booking(client, db_session, admins, pushes):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    for status in (BookingStatus.ACCEPTED, BookingStatus.COMPLETED, BookingStatus.EXPIRED):
        _, booking = await _booking(db_session, service=service, provider_id=sp.id, status=status)
        resp = await client.post(RETRY.format(booking.id), headers=_session(admins.support))
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "BOOKING_NOT_REQUESTED"
        assert resp.json()["details"] == {"status": status.value}
    resp = await client.post(RETRY.format(uuid.uuid4()), headers=_session(admins.support))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retry_dispatch_double_click_is_refused(client, db_session, admins, pushes):
    service = await _service(db_session)
    await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    first = await client.post(RETRY.format(booking.id), headers=_session(admins.support))
    assert first.status_code == 200
    second = await client.post(RETRY.format(booking.id), headers=_session(admins.admin))
    assert second.status_code == 409
    assert second.json()["code"] == "DISPATCH_RETRY_TOO_SOON"
    assert 1 <= second.json()["details"]["retry_after_seconds"] <= 30
    assert len(await _audit(db_session, "booking.retry_dispatch", booking.id)) == 1


@pytest.mark.asyncio
async def test_offer_expiry_sql_matches_python(db_session):
    """The sweep reads the SQL expression, the countdown the Python one. They
    must agree for every way a window can start."""
    now = datetime.now(UTC).replace(microsecond=0)
    cases = {
        "plain": {},
        "reopened": {"reopened_at": now - timedelta(seconds=30)},
        "retried": {
            "reopened_at": now - timedelta(seconds=90),
            "dispatch_retried_at": now - timedelta(seconds=10),
        },
        "retried, then its last chance": {
            "dispatch_retried_at": now - timedelta(seconds=200),
            "last_chance_at": now - timedelta(seconds=20),
        },
        "reopened after a retry": {
            "dispatch_retried_at": now - timedelta(seconds=90),
            "reopened_at": now - timedelta(seconds=5),
        },
    }
    for name, fields in cases.items():
        _, booking = await _booking(db_session)
        booking.created_at = now - timedelta(minutes=5)
        for key, value in fields.items():
            setattr(booking, key, value)
        await db_session.flush()
        in_sql = await db_session.scalar(
            select(Booking.offer_expires_at).where(Booking.id == booking.id)
        )
        start_sql = await db_session.scalar(
            select(Booking.offer_window_start).where(Booking.id == booking.id)
        )
        assert in_sql == booking.offer_expires_at, name
        assert start_sql == booking.offer_window_start, name


def test_the_real_sweep_spares_a_retried_booking(committed_sweep_world):  # noqa: F811
    """Through ``expire_stale_bookings`` itself: an unpaid booking 3 minutes old
    expires, unless support retried it a moment ago."""
    sync_factory, made = committed_sweep_world
    left = _seed(sync_factory, made, paid=False, age=180)
    retried = _seed(
        sync_factory,
        made,
        paid=False,
        age=180,
        dispatch_retried_at=datetime.now(UTC) - timedelta(seconds=5),
    )

    _sweep()

    assert _get(sync_factory, left).status is BookingStatus.EXPIRED
    assert _get(sync_factory, retried).status is BookingStatus.REQUESTED


# --- Reassign ----------------------------------------------------------------------


async def _team_job(db):
    """An ACCEPTED booking at an SP with its Pro-for-self and three team Pros:
    Rohan on the job, Meera free, Kiran offline."""
    service = await _service(db)
    sp, self_pro, rohan = await _team_sp(db, service)
    meera = await _team_pro(db, sp.id, name="Meera")
    kiran = await _team_pro(db, sp.id, name="Kiran", online=False)
    _, booking = await _booking(
        db, service=service, provider_id=sp.id, status=BookingStatus.ACCEPTED
    )
    booking.professional_id = rohan.id
    await db.flush()
    return SimpleNamespace(
        service=service,
        sp=sp,
        self_pro=self_pro,
        rohan=rohan,
        meera=meera,
        kiran=kiran,
        booking=booking,
    )


@pytest.mark.asyncio
async def test_reassign_moves_the_job_to_another_pro_of_the_sp(client, db_session, admins, pushes):
    job = await _team_job(db_session)

    resp = await client.post(
        REASSIGN.format(job.booking.id),
        json={"professional_id": str(job.meera.id)},
        headers=_session(admins.support),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"
    await db_session.refresh(job.booking)
    assert job.booking.professional_id == job.meera.id
    assert job.booking.provider_id == job.sp.id

    (event,) = list(
        await db_session.scalars(
            select(BookingAssignmentEvent).where(
                BookingAssignmentEvent.booking_id == job.booking.id
            )
        )
    )
    assert (event.kind, event.from_professional_id, event.to_professional_id) == (
        "repicked",
        job.rohan.id,
        job.meera.id,
    )
    assert event.actor_user_id == admins.support.id

    (row,) = await _audit(db_session, "booking.reassign_pro", job.booking.id)
    assert row.sp_user_id == job.sp.id
    assert row.before_json == {"professional_id": str(job.rohan.id)}
    assert row.after_json == {"professional_id": str(job.meera.id)}

    assert pushes == []
    _run_after_commit(db_session.sync_session)
    # The old Pro is told it was taken off them; the new one gets it. Nobody else.
    assert sorted((e, kw.get("professional_id")) for e, kw in pushes) == [
        ("assigned", None),
        ("unassigned", str(job.rohan.id)),
    ]


@pytest.mark.asyncio
async def test_reassign_refuses_what_assign_refuses_and_the_list_says_so(
    client, db_session, admins, pushes
):
    job = await _team_job(db_session)
    unverified = await _team_pro(db_session, job.sp.id, name="Anil", kyc=KYCStatus.PENDING)
    declined = await _team_pro(db_session, job.sp.id, name="Divya")
    db_session.add(
        BookingAssignmentEvent(
            booking_id=job.booking.id, kind="declined", from_professional_id=declined.id
        )
    )
    busy = await _team_pro(db_session, job.sp.id, name="Bala")
    _, other = await _booking(
        db_session, service=job.service, provider_id=job.sp.id, status=BookingStatus.EN_ROUTE
    )
    other.professional_id = busy.id
    await db_session.flush()
    headers = _session(admins.ops)

    expected = {
        job.kiran.id: "PRO_OFFLINE",
        unverified.id: "PRO_KYC_INCOMPLETE",
        declined.id: "PRO_DECLINED_THIS_BOOKING",
        busy.id: "PRO_BUSY",
    }
    listing = await client.get(ELIGIBLE.format(job.booking.id), headers=headers)
    assert listing.status_code == 200, listing.text
    rows = {uuid.UUID(r["professional_id"]): r for r in listing.json()["pros"]}
    assert rows[job.rohan.id]["is_current"] and not rows[job.rohan.id]["eligible"]
    assert rows[job.meera.id]["eligible"] and rows[job.meera.id]["refusal_code"] is None
    assert rows[job.self_pro.id]["eligible"] and rows[job.self_pro.id]["is_self"]

    # Each refusal in the list is exactly what the POST answers.
    for pro_id, code in expected.items():
        assert rows[pro_id]["refusal_code"] == code
        resp = await client.post(
            REASSIGN.format(job.booking.id), json={"professional_id": str(pro_id)}, headers=headers
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == code

    await db_session.refresh(job.booking)
    assert job.booking.professional_id == job.rohan.id
    assert await _audit(db_session, "booking.reassign_pro", job.booking.id) == []


@pytest.mark.asyncio
async def test_reassign_stays_within_the_same_sp(client, db_session, admins, pushes):
    job = await _team_job(db_session)
    _, _, stranger = await _team_sp(db_session, job.service)
    resp = await client.post(
        REASSIGN.format(job.booking.id),
        json={"professional_id": str(stranger.id)},
        headers=_session(admins.admin),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "PRO_NOT_FOUND"
    listing = await client.get(ELIGIBLE.format(job.booking.id), headers=_session(admins.admin))
    assert str(stranger.id) not in {r["professional_id"] for r in listing.json()["pros"]}


@pytest.mark.asyncio
async def test_reassign_only_on_an_accepted_booking(client, db_session, admins, pushes):
    job = await _team_job(db_session)
    headers = _session(admins.support)
    for status in (BookingStatus.REQUESTED, BookingStatus.EN_ROUTE, BookingStatus.COMPLETED):
        job.booking.status = status
        await db_session.flush()
        for resp in (
            await client.post(
                REASSIGN.format(job.booking.id),
                json={"professional_id": str(job.meera.id)},
                headers=headers,
            ),
            await client.get(ELIGIBLE.format(job.booking.id), headers=headers),
        ):
            assert resp.status_code == 409, resp.text
            assert resp.json()["code"] == "BOOKING_NOT_REASSIGNABLE"
    _, unclaimed = await _booking(db_session, service=job.service)
    resp = await client.get(ELIGIBLE.format(unclaimed.id), headers=headers)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_reassign_to_the_same_pro_changes_nothing(client, db_session, admins, pushes):
    job = await _team_job(db_session)
    resp = await client.post(
        REASSIGN.format(job.booking.id),
        json={"professional_id": str(job.rohan.id)},
        headers=_session(admins.support),
    )
    assert resp.status_code == 200
    assert await _audit(db_session, "booking.reassign_pro", job.booking.id) == []
    _run_after_commit(db_session.sync_session)
    assert pushes == []


@pytest.mark.asyncio
async def test_reassign_fills_a_job_left_with_no_pro(client, db_session, admins, pushes):
    """A team Pro declined and the SP hasn't re-picked: support can."""
    job = await _team_job(db_session)
    job.booking.professional_id = None
    await db_session.flush()
    resp = await client.post(
        REASSIGN.format(job.booking.id),
        json={"professional_id": str(job.meera.id)},
        headers=_session(admins.support),
    )
    assert resp.status_code == 200, resp.text
    _run_after_commit(db_session.sync_session)
    assert [e for e, _ in pushes] == ["assigned"]


# --- Every role gets 2xx -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "ops", "support"])
async def test_every_admin_role_can_use_the_console(client, db_session, admins, pushes, role):
    headers = _session(getattr(admins, role))
    service = await _service(db_session)
    await _eligible_provider(db_session, service)
    consumer, waiting = await _booking(db_session, service=service)
    job = await _team_job(db_session)

    calls = [
        ("GET", LOOKUP + f"?q={consumer.phone[3:]}", None),
        ("GET", CUSTOMER.format(consumer.id), None),
        ("POST", NOTES.format(waiting.id), {"body": "hello"}),
        ("POST", CONTACTED.format(waiting.id), None),
        ("GET", NOTES.format(waiting.id), None),
        ("POST", RETRY.format(waiting.id), None),
        ("GET", ELIGIBLE.format(job.booking.id), None),
        ("POST", REASSIGN.format(job.booking.id), {"professional_id": str(job.meera.id)}),
    ]
    for method, url, body in calls:
        resp = await client.request(method, url, json=body, headers=headers)
        assert 200 <= resp.status_code < 300, f"{role} {method} {url}: {resp.text}"
