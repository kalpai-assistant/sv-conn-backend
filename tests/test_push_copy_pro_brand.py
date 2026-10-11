"""
SVC-330: consumer pushes name the Pro on the job, and their business.

In-app screens show "Rohan from Sparkle Cleaners" (SVC-324). The pushes about
the same booking said "Your provider…", so a consumer with the phone locked
never saw who was coming. The bodies of the four pushes about the person at
the door now carry the same name; the SP- and Pro-facing pushes are untouched.

The task-level tests run the real Celery task against committed rows, like
test_booking_push_recipients.py, so the join from the Pro to the SP's
business name is real SQL rather than a mock.
"""

from unittest.mock import patch

import pytest

from app.models.booking import BookingStatus
from app.models.professional import Professional
from app.models.provider_profile import ProviderProfile
from app.tasks.notification_tasks import (
    NOTIFICATION_TEMPLATES,
    PUSH_BODY_MAX_CHARS,
    build_consumer_body,
    consumer_push_who,
    send_booking_notification,
)
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_booking_push_recipients import _address

NAMED_EVENTS = ["accepted", "en_route", "in_progress", "completed"]
LONG_BRAND = "Sparkle Cleaning Services Pvt Ltd"


# ---------------------------------------------------------------------------
# The "who" helper
# ---------------------------------------------------------------------------


def test_team_pro_with_a_brand_is_named_with_it():
    assert (
        consumer_push_who("Rohan", "Sparkle Cleaners", is_self=False, max_len=60)
        == "Rohan from Sparkle Cleaners"
    )


def test_solo_sp_is_the_name_alone():
    """The Pro-for-self: the person is the business, so naming both repeats them."""
    assert consumer_push_who("Sunita", "Sunita's Salon", is_self=True, max_len=60) == "Sunita"


@pytest.mark.parametrize("brand", [None, "", "   "])
def test_no_business_name_is_the_name_alone(brand):
    assert consumer_push_who("Rohan", brand, is_self=False, max_len=60) == "Rohan"


def test_a_brand_that_is_just_the_name_is_not_repeated():
    assert consumer_push_who("Rohan", " rohan ", is_self=False, max_len=60) == "Rohan"


@pytest.mark.parametrize("name", [None, "", "  "])
def test_no_name_means_nothing_to_name(name):
    assert consumer_push_who(name, "Sparkle Cleaners", is_self=False, max_len=60) is None


def test_a_long_brand_is_cut_with_an_ellipsis():
    who = consumer_push_who("Rohan", LONG_BRAND, is_self=False, max_len=30)

    assert who.startswith("Rohan from Sparkle")
    assert who.endswith("…")
    assert len(who) <= 30


def test_too_little_room_for_the_brand_drops_it():
    """ "Rohan from Sp…" says nothing; the name alone does."""
    assert consumer_push_who("Rohan", LONG_BRAND, is_self=False, max_len=14) == "Rohan"


def test_an_absurd_name_is_cut_too():
    who = consumer_push_who("R" * 200, "Sparkle", is_self=False, max_len=40)

    assert len(who) == 40
    assert who.endswith("…")


# ---------------------------------------------------------------------------
# The whole body, against the shade budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event", NAMED_EVENTS)
def test_no_pro_keeps_the_generic_copy(event):
    template = NOTIFICATION_TEMPLATES[event]

    assert build_consumer_body(template, None) == template["body"]


@pytest.mark.parametrize("event", NAMED_EVENTS)
@pytest.mark.parametrize(
    "parts",
    [
        ("Rohan", LONG_BRAND, False),
        ("Rohan Venkataraman Subramaniam", LONG_BRAND, False),
        ("R" * 120, LONG_BRAND, False),
        ("R" * 120, None, True),
    ],
)
def test_every_named_body_fits_the_shade(event, parts):
    """The final body, "…" included, never passes the budget."""
    body = build_consumer_body(NOTIFICATION_TEMPLATES[event], parts)

    assert len(body) <= PUSH_BODY_MAX_CHARS, body
    assert body.startswith("R")


def test_the_34_char_brand_is_truncated_where_it_does_not_fit():
    body = build_consumer_body(
        NOTIFICATION_TEMPLATES["completed"], ("Rohan Venkataraman", LONG_BRAND, False)
    )

    assert body.startswith("Rohan Venkataraman from Sparkle")
    assert "…" in body
    assert len(body) <= PUSH_BODY_MAX_CHARS


# ---------------------------------------------------------------------------
# The real task
# ---------------------------------------------------------------------------


@pytest.fixture
async def committed(db_session):
    """Commit test rows (the task has its own connection), then remove them."""
    created: list = []
    yield created
    for obj in reversed(created):
        await db_session.delete(obj)
    await db_session.commit()


def _pushes(booking_id, event, actor_id=None):
    """Run the real task; capture (token, title, body) for every push."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_url = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url)
    sent = []
    with (
        patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)),
        patch("app.tasks.notification_tasks.send_push_notification") as mock_push,
    ):
        mock_push.side_effect = lambda token, title, body, **_: sent.append((token, title, body))
        send_booking_notification(booking_id=str(booking_id), event=event, actor_id=actor_id)
    engine.dispose()
    return sent


async def _job(db, committed, *, brand, team, status=BookingStatus.ACCEPTED):
    """A booking held by an SP and done by its Pro-for-self or a team Pro "Rohan".

    Every party has its own token so a push to the wrong person shows up. The
    SP's profile row is real and its ``id`` differs from its ``user_id``, so a
    join on the wrong column finds no brand and the team tests fail.
    """
    consumer = await UserFactory.create(db, role="consumer", fcm_token="consumer-token")
    sp = await UserFactory.create(db, role="provider", name="Sunita", fcm_token="sp-token")
    member = await UserFactory.create(db, role="consumer", name="Rohan", fcm_token="pro-token")
    sp_profile = ProviderProfile(user_id=sp.id, business_name=brand)
    db.add(sp_profile)
    await db.flush()
    self_pro = Professional(service_provider_id=sp.id, user_id=sp.id, display_name="Sunita")
    team_pro = Professional(service_provider_id=sp.id, user_id=member.id, display_name="Rohan")
    db.add_all([self_pro, team_pro])
    await db.flush()
    addr = await _address(db, consumer.id)
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=sp.id,
        address_id=addr.id,
        status=status,
    )
    booking.professional_id = (team_pro if team else self_pro).id
    # Deleted in reverse: the booking first, the users last.
    committed.extend([consumer, sp, member, sp_profile, self_pro, team_pro, addr, booking])
    await db.commit()
    return booking, sp, member


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "status", "expected"),
    [
        (
            "accepted",
            BookingStatus.ACCEPTED,
            "Rohan from Sparkle Cleaners will complete your order.",
        ),
        ("en_route", BookingStatus.EN_ROUTE, "Rohan from Sparkle Cleaners is on the way to you."),
        (
            "in_progress",
            BookingStatus.IN_PROGRESS,
            "Rohan from Sparkle Cleaners has started your service.",
        ),
        (
            "completed",
            BookingStatus.COMPLETED,
            "Rohan from Sparkle Cleaners has completed your service. Please proceed to payment.",
        ),
    ],
)
async def test_a_team_pro_is_named_with_the_brand(
    db_session,
    committed,
    event,
    status,
    expected,
):
    booking, _, _ = await _job(
        db_session, committed, brand="Sparkle Cleaners", team=True, status=status
    )

    assert _pushes(booking.id, event) == [
        ("consumer-token", NOTIFICATION_TEMPLATES[event]["title"], expected)
    ]


@pytest.mark.asyncio
async def test_a_solo_job_names_the_person_alone(db_session, committed):
    booking, _, _ = await _job(db_session, committed, brand="Sunita's Salon", team=False)

    [(_, _, body)] = _pushes(booking.id, "accepted")

    assert body == "Sunita will complete your order."


@pytest.mark.asyncio
async def test_a_team_pro_without_a_brand_is_named_alone(db_session, committed):
    booking, _, _ = await _job(db_session, committed, brand=None, team=True)

    [(_, _, body)] = _pushes(booking.id, "en_route")

    assert body == "Rohan is on the way to you."


@pytest.mark.asyncio
async def test_a_long_brand_still_fits(db_session, committed):
    booking, _, _ = await _job(
        db_session, committed, brand=LONG_BRAND, team=True, status=BookingStatus.COMPLETED
    )

    [(_, _, body)] = _pushes(booking.id, "completed")

    assert body.startswith("Rohan from Sparkle")
    assert len(body) <= PUSH_BODY_MAX_CHARS


@pytest.mark.asyncio
async def test_no_pro_keeps_the_generic_push(db_session, committed):
    booking, _, _ = await _job(db_session, committed, brand="Sparkle Cleaners", team=True)
    booking.professional_id = None
    await db_session.commit()

    assert _pushes(booking.id, "accepted") == [
        ("consumer-token", "Booking Confirmed", NOTIFICATION_TEMPLATES["accepted"]["body"])
    ]


# --- SP- and Pro-facing pushes are not this ticket's -------------------------------


@pytest.mark.asyncio
async def test_the_assigned_push_to_the_pro_is_unchanged(db_session, committed):
    booking, _, _ = await _job(db_session, committed, brand="Sparkle Cleaners", team=True)

    assert _pushes(booking.id, "assigned") == [
        ("pro-token", "Job assigned", "You've been assigned a new job.")
    ]


@pytest.mark.asyncio
async def test_the_pro_declined_push_to_the_sp_is_unchanged(db_session, committed):
    booking, _, member = await _job(db_session, committed, brand="Sparkle Cleaners", team=True)

    assert _pushes(booking.id, "pro_declined", actor_id=str(member.id)) == [
        ("sp-token", "Pick another professional", "Rohan can't take this job. Assign someone else.")
    ]


@pytest.mark.asyncio
async def test_the_cancelled_push_is_unchanged(db_session, committed):
    booking, sp, _ = await _job(db_session, committed, brand="Sparkle Cleaners", team=True)

    # The team Pro's own push is SVC-348's (test_pro_loss_push.py).
    assert _pushes(booking.id, "cancelled", actor_id=str(sp.id)) == [
        ("consumer-token", "Booking Cancelled", NOTIFICATION_TEMPLATES["cancelled"]["body"]),
        ("pro-token", "Job cancelled", NOTIFICATION_TEMPLATES["job_cancelled"]["body"]),
    ]
