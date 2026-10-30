"""Coverage for SVC-388's ``latest_review_snippet`` on ``GET /services``.

Vipul's rules (Slack, 2026-10-30):

- Gated like the SVC-400 rating: fewer than ``MIN_REVIEWS_FOR_RATING``
  qualifying reviews (the consumer's own, on a single-service booking) → null.
- Eligible: a qualifying review with rating >= 4 and a trimmed comment longer
  than 20 chars. The newest wins.
- Text cut to 80 chars on a word boundary, "…" included in the 80.
- A deleted or nameless reviewer stays in with ``reviewer_first_name`` null.
- One query for the whole listing, never one per service.

The inputs are stored on ``services`` (count + pointer) and recomputed when a
review is created. Most tests insert reviews directly, to control
``created_at`` and the reviewer, then run ``review_stats_update`` for the
booking's services, the statement both the write path and the backfill use.
The write-path tests go through ``ReviewService.create_review``, and the
backfill is checked against the old live query, kept here as the oracle.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.booking_item import BookingItem
from app.models.category import Category
from app.models.review import Review
from app.models.service import Service
from app.models.user import User
from app.services.catalog_service import (
    _WHITESPACE,
    MIN_REVIEWS_FOR_RATING,
    SNIPPET_MAX_CHARS,
    SNIPPET_MIN_CHARS,
    SNIPPET_MIN_RATING,
    _qualifying_reviews,
    _snippet_text,
    review_stats_update,
)
from app.services.review_service import ReviewService
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import ADDRESS_POINT, _service

LIST = "/api/v1/services?category_id={}"
DETAIL = "/api/v1/services/{}"
# Every review in one test transaction would otherwise share now(); explicit
# timestamps make "newest" mean something.
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
GOOD = "Finally the clean I like, on time and very thorough."


async def _booking(db, service_ids, *, consumer=None):
    consumer = consumer or await UserFactory.create(db, role="consumer", name="Priya Sharma")
    provider = await UserFactory.create(db, role="provider")
    address = Address(
        user_id=consumer.id, label="home", full_address="12 MG Road", location=ADDRESS_POINT
    )
    db.add(address)
    await db.flush()
    return await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        item_service_ids=service_ids,
        address_id=address.id,
        status=BookingStatus.COMPLETED,
    )


async def _review(db, booking, rating=5, comment=None, *, minutes=0, reviewer_id=None):
    db.add(
        Review(
            booking_id=booking.id,
            reviewer_id=reviewer_id or booking.consumer_id,
            reviewee_id=booking.provider_id,
            rating=rating,
            comment=comment,
            created_at=T0 + timedelta(minutes=minutes),
        )
    )
    await db.flush()
    await _refresh(db, booking)


async def _refresh(db, booking):
    service_ids = (
        (
            await db.execute(
                select(BookingItem.service_id).where(BookingItem.booking_id == booking.id)
            )
        )
        .scalars()
        .all()
    )
    await db.execute(
        review_stats_update(list(service_ids)),
        execution_options={"synchronize_session": "fetch"},
    )


async def _qualify(db, service, n, *, rating=5):
    """n qualifying reviews with no comment: they count toward the gate but
    can never be the snippet."""
    for _ in range(n):
        await _review(db, await _booking(db, [service.id]), rating)


async def _snippet(client, service):
    response = await client.get(LIST.format(service.category_id))
    assert response.status_code == 200, response.text
    [row] = [s for s in response.json() if s["id"] == str(service.id)]
    return row["latest_review_snippet"]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_service_without_reviews_has_a_null_snippet(client, db_session):
    service = await _service(db_session)

    assert await _snippet(client, service) is None


@pytest.mark.asyncio
async def test_nine_qualifying_reviews_give_null_and_the_tenth_shows_it(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING - 2)
    await _review(db_session, await _booking(db_session, [service.id]), 5, GOOD)

    assert await _snippet(client, service) is None

    await _qualify(db_session, service, 1)
    snippet = await _snippet(client, service)

    assert snippet == {
        "text": GOOD,
        "reviewer_first_name": "Priya",
        "created_at": "2026-10-01T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_three_star_review_is_never_the_snippet_but_four_is(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    await _review(db_session, await _booking(db_session, [service.id]), 4, "Four stars, " + GOOD)
    await _review(
        db_session, await _booking(db_session, [service.id]), 3, "Three stars, " + GOOD, minutes=5
    )

    snippet = await _snippet(client, service)

    assert snippet["text"].startswith("Four stars")


@pytest.mark.asyncio
async def test_a_comment_of_twenty_chars_or_less_after_trimming_is_skipped(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    await _review(db_session, await _booking(db_session, [service.id]), 5, GOOD)
    # 20 chars once trimmed — padding and newlines don't make it longer.
    short = "  \n Good work, thanks!!!  \n"
    assert len(short.strip()) == 20
    await _review(db_session, await _booking(db_session, [service.id]), 5, short, minutes=5)
    await _review(db_session, await _booking(db_session, [service.id]), 5, "nice", minutes=6)

    assert (await _snippet(client, service))["text"] == GOOD


@pytest.mark.asyncio
async def test_twenty_one_chars_is_long_enough(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    text = "Good work, thank you!"
    assert len(text) == 21
    await _review(db_session, await _booking(db_session, [service.id]), 5, text)

    assert (await _snippet(client, service))["text"] == text


@pytest.mark.asyncio
async def test_a_multi_service_bookings_review_is_never_the_snippet(client, db_session):
    service = await _service(db_session)
    other = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING - 1)
    await _review(db_session, await _booking(db_session, [service.id, other.id]), 5, GOOD)

    # Nor does it count toward the gate: 9 + the cart review is still 9.
    assert await _snippet(client, service) is None

    await _qualify(db_session, service, 1)
    assert await _snippet(client, service) is None


@pytest.mark.asyncio
async def test_a_review_not_written_by_the_consumer_is_never_the_snippet(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    booking = await _booking(db_session, [service.id])
    await _review(db_session, booking, 5, GOOD, reviewer_id=booking.provider_id)

    assert await _snippet(client, service) is None


# ---------------------------------------------------------------------------
# Selection + rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_newest_eligible_review_wins(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    for minutes, who in ((5, "Older"), (30, "Newest"), (10, "Middle")):
        consumer = await UserFactory.create(db_session, role="consumer", name=f"{who} Person")
        booking = await _booking(db_session, [service.id], consumer=consumer)
        await _review(db_session, booking, 5, f"{who}: {GOOD}", minutes=minutes)

    snippet = await _snippet(client, service)

    assert snippet["text"].startswith("Newest:")
    assert snippet["reviewer_first_name"] == "Newest"
    assert snippet["created_at"] == "2026-10-01T12:30:00Z"


@pytest.mark.asyncio
async def test_a_deleted_reviewer_stays_in_with_a_null_name(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    # What AccountService.delete_account leaves behind (SVC-367).
    consumer = await UserFactory.create(db_session, role="consumer", name="Priya Sharma")
    booking = await _booking(db_session, [service.id], consumer=consumer)
    await _review(db_session, booking, 5, GOOD)
    consumer.name = None
    consumer.deleted_at = T0
    await db_session.flush()

    snippet = await _snippet(client, service)

    assert snippet["text"] == GOOD
    assert snippet["reviewer_first_name"] is None


@pytest.mark.asyncio
async def test_a_deleted_reviewer_whose_name_survived_still_gets_null(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    consumer = await UserFactory.create(db_session, role="consumer", name="Priya Sharma")
    await _review(db_session, await _booking(db_session, [service.id], consumer=consumer), 5, GOOD)
    consumer.deleted_at = T0
    await db_session.flush()

    assert (await _snippet(client, service))["reviewer_first_name"] is None


@pytest.mark.asyncio
async def test_a_blank_name_gives_a_null_first_name(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    consumer = await UserFactory.create(db_session, role="consumer", name="   ")
    await _review(db_session, await _booking(db_session, [service.id], consumer=consumer), 5, GOOD)

    assert (await _snippet(client, service))["reviewer_first_name"] is None


@pytest.mark.asyncio
async def test_a_long_comment_is_cut_on_a_word_boundary_with_an_ellipsis(client, db_session):
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    long = (
        "The team arrived exactly on time, moved every piece of furniture and "
        "cleaned behind it, then mopped twice."
    )
    await _review(db_session, await _booking(db_session, [service.id]), 5, long)

    text = (await _snippet(client, service))["text"]

    assert len(text) <= SNIPPET_MAX_CHARS
    assert text == ("The team arrived exactly on time, moved every piece of furniture and cleaned…")


def test_snippet_text_rules():
    assert (
        _snippet_text("  Short and\n\nsweet, really great.  ") == "Short and sweet, really great."
    )
    exactly = "x" * SNIPPET_MAX_CHARS
    assert _snippet_text(exactly) == exactly
    # A cut that lands on a space keeps the whole last word.
    words = ("abcd " * 20).strip()
    assert _snippet_text(words) == ("abcd " * 16).strip() + "…"
    # One unbroken word is cut hard, still within the limit.
    hard = _snippet_text("y" * 200)
    assert hard == "y" * (SNIPPET_MAX_CHARS - 1) + "…"
    # Trailing punctuation before the ellipsis is dropped.
    assert _snippet_text("a" * 70 + ", bbbbbbbbbbbbbbbbbb").endswith("a…")


# ---------------------------------------------------------------------------
# Query shape + other routes
# ---------------------------------------------------------------------------


async def _category_with_services(db, n):
    category = Category(name=f"Cat {T0.timestamp()}-{n}", slug=f"svc388-{n}", sort_order=1)
    db.add(category)
    await db.flush()
    services = []
    for i in range(n):
        service = Service(
            category_id=category.id,
            name=f"Service {i}",
            base_price=499.0,
            duration_minutes=60,
            is_active=True,
        )
        db.add(service)
        services.append(service)
    await db.flush()
    for service in services:
        await _qualify(db, service, MIN_REVIEWS_FOR_RATING - 1)
        await _review(db, await _booking(db, [service.id]), 5, GOOD)
    return category


@pytest.mark.asyncio
async def test_the_list_costs_the_same_queries_for_one_service_or_many(client, db_session):
    one = await _category_with_services(db_session, 1)
    many = await _category_with_services(db_session, 4)

    async def statements(category):
        executed: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            executed.append(statement)

        event.listen(Engine, "before_cursor_execute", record)
        try:
            response = await client.get(LIST.format(category.id))
        finally:
            event.remove(Engine, "before_cursor_execute", record)
        assert response.status_code == 200, response.text
        rows = response.json()
        assert all(r["latest_review_snippet"]["text"] == GOOD for r in rows)
        return len(executed), len(rows)

    one_count, one_rows = await statements(one)
    many_count, many_rows = await statements(many)

    assert (one_rows, many_rows) == (1, 4)
    assert one_count == many_count


@pytest.mark.asyncio
async def test_detail_inherits_the_field_but_leaves_it_null(client, db_session):
    """Filled on the list only (frontend reads it there); see ServiceResponse."""
    service = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    await _review(db_session, await _booking(db_session, [service.id]), 5, GOOD)

    response = await client.get(DETAIL.format(service.id))

    assert response.status_code == 200, response.text
    assert response.json()["latest_review_snippet"] is None
    assert await _snippet(client, service) is not None


@pytest.mark.asyncio
async def test_rating_is_unchanged_for_two_services_sharing_a_cart(client, db_session):
    """The shared helper correlates on the joined booking_item's service; a
    cart holding both services counts for neither."""
    service = await _service(db_session)
    other = await _service(db_session)
    await _qualify(db_session, service, MIN_REVIEWS_FOR_RATING)
    await _qualify(db_session, other, MIN_REVIEWS_FOR_RATING, rating=4)
    await _review(db_session, await _booking(db_session, [service.id, other.id]), 1, GOOD)

    for svc, average in ((service, 5.0), (other, 4.0)):
        body = (await client.get(DETAIL.format(svc.id))).json()
        assert (body["rating_average"], body["rating_count"]) == (average, 10)


# ---------------------------------------------------------------------------
# Write path: ReviewService.create_review keeps the stored stats current
# ---------------------------------------------------------------------------


async def _stored(db, service):
    await db.refresh(service)
    return service.qualifying_review_count, service.latest_snippet_review_id


@pytest.mark.asyncio
async def test_creating_an_eligible_review_sets_the_count_and_the_pointer(db_session):
    service = await _service(db_session)
    booking = await _booking(db_session, [service.id])

    review = await ReviewService(db_session).create_review(booking.id, booking.consumer_id, 5, GOOD)

    assert await _stored(db_session, service) == (1, review.id)


@pytest.mark.asyncio
async def test_an_ineligible_review_bumps_the_count_and_leaves_the_pointer(db_session):
    service = await _service(db_session)
    first = await _booking(db_session, [service.id])
    eligible = await ReviewService(db_session).create_review(first.id, first.consumer_id, 4, GOOD)
    # Older than anything created later in this transaction (now() is fixed
    # per transaction, and the tie would fall to the random id).
    eligible.created_at = T0
    await db_session.flush()

    for rating, comment in ((3, "Three stars, " + GOOD), (5, "nice"), (5, None)):
        booking = await _booking(db_session, [service.id])
        await ReviewService(db_session).create_review(
            booking.id, booking.consumer_id, rating, comment
        )

    assert await _stored(db_session, service) == (4, eligible.id)

    newer = await _booking(db_session, [service.id])
    moved = await ReviewService(db_session).create_review(newer.id, newer.consumer_id, 5, GOOD)

    assert await _stored(db_session, service) == (5, moved.id)


@pytest.mark.asyncio
async def test_a_multi_service_bookings_review_changes_nothing(db_session):
    service = await _service(db_session)
    other = await _service(db_session)
    booking = await _booking(db_session, [service.id, other.id])

    await ReviewService(db_session).create_review(booking.id, booking.consumer_id, 5, GOOD)

    assert await _stored(db_session, service) == (0, None)
    assert await _stored(db_session, other) == (0, None)


# ---------------------------------------------------------------------------
# Backfill: review_stats_update over existing reviews == the old live query
# ---------------------------------------------------------------------------


async def _live_oracle(db, service_ids):
    """The pre-denormalisation live query (SVC-388's first cut): ungated
    qualifying count, and the gated newest eligible snippet, per service."""
    qualifying = (
        _qualifying_reviews(
            BookingItem.service_id.label("service_id"),
            Review.id.label("review_id"),
            Review.rating,
            Review.comment,
            Review.created_at,
            Review.reviewer_id,
        )
        .where(BookingItem.service_id.in_(service_ids))
        .cte("qualifying")
    )
    counts = dict(
        (
            await db.execute(
                select(qualifying.c.service_id, func.count()).group_by(qualifying.c.service_id)
            )
        ).all()
    )
    gated = (
        select(qualifying.c.service_id)
        .group_by(qualifying.c.service_id)
        .having(func.count() >= MIN_REVIEWS_FOR_RATING)
        .cte("gated")
    )
    rows = await db.execute(
        select(qualifying.c.service_id, qualifying.c.comment, qualifying.c.created_at, User.name)
        .select_from(qualifying)
        .join(gated, gated.c.service_id == qualifying.c.service_id)
        .join(User, User.id == qualifying.c.reviewer_id)
        .where(
            qualifying.c.rating >= SNIPPET_MIN_RATING,
            func.length(func.btrim(qualifying.c.comment, _WHITESPACE)) > SNIPPET_MIN_CHARS,
        )
        .distinct(qualifying.c.service_id)
        .order_by(
            qualifying.c.service_id,
            qualifying.c.created_at.desc(),
            qualifying.c.review_id.desc(),
        )
    )
    snippets = {
        row.service_id: {
            "text": _snippet_text(row.comment),
            "first": row.name.split()[0] if row.name and row.name.strip() else None,
            "created_at": row.created_at,
        }
        for row in rows
    }
    return counts, snippets


@pytest.mark.asyncio
async def test_the_backfill_matches_the_old_live_query(client, db_session):
    rng = random.Random(388)
    category = Category(name="SVC-388 oracle", slug="svc388-oracle", sort_order=1)
    db_session.add(category)
    await db_session.flush()
    services = [
        Service(
            category_id=category.id,
            name=f"Oracle {i}",
            base_price=499.0,
            duration_minutes=60,
            is_active=True,
        )
        for i in range(5)
    ]
    db_session.add_all(services)
    await db_session.flush()
    comments = [None, "nice", "  \n Good work, thanks!!!  \n", GOOD, "A" * 30 + " " + GOOD * 2]
    for _ in range(140):
        service = rng.choice(services)
        ids = [service.id]
        if rng.random() < 0.15:
            ids.append(rng.choice([s.id for s in services if s is not service]))
        consumer = await UserFactory.create(
            db_session, role="consumer", name=rng.choice(["Asha Rao", "  ", "Ravi"])
        )
        booking = await _booking(db_session, ids, consumer=consumer)
        db_session.add(
            Review(
                booking_id=booking.id,
                reviewer_id=(booking.provider_id if rng.random() < 0.05 else booking.consumer_id),
                reviewee_id=booking.provider_id,
                rating=rng.randint(1, 5),
                comment=rng.choice(comments),
                # Coarse minutes, so some created_at ties fall to the id.
                created_at=T0 + timedelta(minutes=rng.randint(0, 40)),
            )
        )
    await db_session.flush()
    service_ids = [s.id for s in services]

    await db_session.execute(
        review_stats_update(service_ids), execution_options={"synchronize_session": "fetch"}
    )

    counts, expected = await _live_oracle(db_session, service_ids)
    assert any(c >= MIN_REVIEWS_FOR_RATING for c in counts.values())
    assert expected, "the seed should give at least one service a snippet"
    response = await client.get(LIST.format(category.id))
    assert response.status_code == 200, response.text
    by_id = {row["id"]: row["latest_review_snippet"] for row in response.json()}
    for service in services:
        await db_session.refresh(service)
        assert service.qualifying_review_count == counts.get(service.id, 0)
        want = expected.get(service.id)
        got = by_id[str(service.id)]
        if want is None:
            assert got is None, service.name
        else:
            assert got["text"] == want["text"]
            assert got["reviewer_first_name"] == want["first"]
            assert datetime.fromisoformat(got["created_at"]) == want["created_at"]
