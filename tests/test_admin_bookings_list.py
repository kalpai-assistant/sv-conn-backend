"""SVC-425: GET /admin/bookings order and filters.

Rows other test files commit for real land at "now", so each test here puts its
own bookings in 2099. Newest first then puts them at the top, and a 2099 date or a
unique SP name or phone scopes ``total`` to this test's own rows.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.user import AdminRole
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

URL = "/api/v1/admin/bookings"
BASE = datetime(2099, 6, 1, 6, 0, tzinfo=UTC)


async def _headers(db, auth_headers, role: str = "admin") -> dict:
    admin = await UserFactory.create(db, role="admin")
    admin.admin_role = AdminRole(role)
    await db.flush()
    return auth_headers(str(admin.id), "admin")


async def _booking(db, consumer, provider, created_at, status=BookingStatus.COMPLETED):
    addr = Address(user_id=consumer.id, label="home", full_address="Addr")
    db.add(addr)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id if provider else None,
        address_id=addr.id,
        status=status,
    )
    booking.created_at = created_at
    await db.flush()
    return booking


async def _dispute(db, booking):
    db.add(
        Payment(
            booking_id=booking.id,
            razorpay_payment_id=f"pay_{uuid.uuid4().hex[:10]}",
            razorpay_order_id=f"order_{uuid.uuid4().hex[:10]}",
            amount=Decimal("100"),
            platform_fee=Decimal("20"),
            provider_settlement=Decimal("80"),
            status=PaymentRecordStatus.CAPTURED,
            method=PaymentRecordMethod.UPI,
            disputed_at=datetime.now(UTC),
        )
    )
    await db.flush()


def _token() -> str:
    return f"sp{uuid.uuid4().hex[:8]}"


def _ids(body) -> list[str]:
    return [b["id"] for b in body["bookings"]]


async def test_newest_first_and_ties_break_on_id_across_pages(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    tok = _token()
    sp = await UserFactory.create(db_session, role="provider", name=f"{tok} Services")
    consumer = await UserFactory.create(db_session)
    same = BASE + timedelta(hours=1)
    made = [
        await _booking(db_session, consumer, sp, BASE),
        await _booking(db_session, consumer, sp, same),
        await _booking(db_session, consumer, sp, same),
        await _booking(db_session, consumer, sp, same),
        await _booking(db_session, consumer, sp, BASE + timedelta(days=2)),
    ]
    expected = [str(b.id) for b in sorted(made, key=lambda b: (b.created_at, b.id), reverse=True)]

    seen = []
    for page in (1, 2, 3):
        r = await client.get(
            URL, params={"provider_name": tok, "per_page": 2, "page": page}, headers=headers
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 5
        seen += _ids(r.json())
    assert seen == expected


async def test_no_filter_puts_newest_at_the_top(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    consumer = await UserFactory.create(db_session)
    sp = await UserFactory.create(db_session, role="provider")
    older = await _booking(db_session, consumer, sp, BASE)
    newer = await _booking(db_session, consumer, sp, BASE + timedelta(minutes=1))

    r = await client.get(URL, params={"per_page": 2}, headers=headers)
    assert r.status_code == 200
    assert _ids(r.json()) == [str(newer.id), str(older.id)]


async def test_status_alone(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    consumer = await UserFactory.create(db_session)
    sp = await UserFactory.create(db_session, role="provider")
    done = await _booking(db_session, consumer, sp, BASE, BookingStatus.COMPLETED)
    live = await _booking(
        db_session, consumer, sp, BASE + timedelta(minutes=1), BookingStatus.EN_ROUTE
    )

    r = await client.get(URL, params={"status": "en_route", "per_page": 200}, headers=headers)
    assert r.status_code == 200
    ids = _ids(r.json())
    assert str(live.id) in ids
    assert str(done.id) not in ids
    assert {b["status"] for b in r.json()["bookings"]} == {"en_route"}


async def test_dates_alone(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    consumer = await UserFactory.create(db_session)
    sp = await UserFactory.create(db_session, role="provider")
    before = await _booking(db_session, consumer, sp, datetime(2099, 3, 1, 6, tzinfo=UTC))
    inside = [
        await _booking(db_session, consumer, sp, datetime(2099, 3, d, 6, tzinfo=UTC))
        for d in (2, 3, 4)
    ]
    after = await _booking(db_session, consumer, sp, datetime(2099, 3, 5, 6, tzinfo=UTC))

    r = await client.get(
        URL, params={"created_from": "2099-03-02", "created_to": "2099-03-04"}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["total"] == 3
    assert _ids(r.json()) == [str(b.id) for b in reversed(inside)]

    only_from = await client.get(URL, params={"created_from": "2099-03-05"}, headers=headers)
    assert str(after.id) in _ids(only_from.json())
    assert str(inside[-1].id) not in _ids(only_from.json())

    only_to = await client.get(
        URL, params={"created_to": "2099-03-01", "per_page": 200}, headers=headers
    )
    assert str(before.id) in _ids(only_to.json())
    assert str(inside[0].id) not in _ids(only_to.json())


async def test_ist_day_edges(client, db_session, auth_headers):
    """IST midnight is 18:30 UTC the day before."""
    headers = await _headers(db_session, auth_headers)
    consumer = await UserFactory.create(db_session)
    sp = await UserFactory.create(db_session, role="provider")
    eve_end = await _booking(db_session, consumer, sp, datetime(2099, 1, 9, 18, 29, 59, tzinfo=UTC))
    day_start = await _booking(db_session, consumer, sp, datetime(2099, 1, 9, 18, 30, tzinfo=UTC))
    day_end = await _booking(
        db_session, consumer, sp, datetime(2099, 1, 10, 18, 29, 59, 999999, tzinfo=UTC)
    )
    next_start = await _booking(db_session, consumer, sp, datetime(2099, 1, 10, 18, 30, tzinfo=UTC))

    r = await client.get(
        URL, params={"created_from": "2099-01-10", "created_to": "2099-01-10"}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["total"] == 2
    assert _ids(r.json()) == [str(day_end.id), str(day_start.id)]
    assert str(eve_end.id) not in _ids(r.json())
    assert str(next_start.id) not in _ids(r.json())


async def test_from_after_to_is_422(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    r = await client.get(
        URL, params={"created_from": "2099-01-11", "created_to": "2099-01-10"}, headers=headers
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "fmt",
    [
        lambda d: f"+91{d}",
        lambda d: f"+91 {d[:5]} {d[5:]}",
        lambda d: f"{d[:5]}-{d[5:]}",
        lambda d: d,
        lambda d: f"0{d}",
        lambda d: f"91{d}",
    ],
    ids=["e164", "spaces", "dashes", "ten-digits", "leading-zero", "91-no-plus"],
)
async def test_consumer_phone_formats(client, db_session, auth_headers, fmt):
    headers = await _headers(db_session, auth_headers)
    consumer = await UserFactory.create(db_session)
    other = await UserFactory.create(db_session)
    sp = await UserFactory.create(db_session, role="provider")
    mine = await _booking(db_session, consumer, sp, BASE)
    await _booking(db_session, other, sp, BASE)

    r = await client.get(URL, params={"consumer_phone": fmt(consumer.phone[3:])}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert _ids(r.json()) == [str(mine.id)]
    assert r.json()["bookings"][0]["consumer_phone"] == consumer.phone


@pytest.mark.parametrize("value", ["", "   ", "98123", "98123 4567"])
async def test_consumer_phone_under_ten_digits_is_422(client, db_session, auth_headers, value):
    headers = await _headers(db_session, auth_headers)
    r = await client.get(URL, params={"consumer_phone": value}, headers=headers)
    assert r.status_code == 422


async def test_provider_name_case_insensitive_substring(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    tok = _token()
    consumer = await UserFactory.create(db_session)
    sp = await UserFactory.create(db_session, role="provider", name=f"Ramesh {tok} Cleaning")
    other = await UserFactory.create(db_session, role="provider", name="Someone Else")
    mine = await _booking(db_session, consumer, sp, BASE)
    await _booking(db_session, consumer, other, BASE)
    await _booking(db_session, consumer, None, BASE, BookingStatus.REQUESTED)

    r = await client.get(URL, params={"provider_name": f"  {tok.upper()} clean "}, headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert _ids(r.json()) == [str(mine.id)]
    assert r.json()["bookings"][0]["provider_name"] == sp.name


async def test_blank_provider_name_is_no_filter(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    consumer = await UserFactory.create(db_session)
    unassigned = await _booking(db_session, consumer, None, BASE, BookingStatus.REQUESTED)

    r = await client.get(URL, params={"provider_name": "  ", "per_page": 5}, headers=headers)
    assert r.status_code == 200
    assert str(unassigned.id) in _ids(r.json())


@pytest.mark.parametrize(
    ("query", "hit", "miss"),
    [
        ("50% off", "50% off", "50x off"),
        ("a_b", "a_b", "axb"),
        ("back\\slash", "back\\slash", "backslash"),
    ],
)
async def test_provider_name_wildcards_are_literal(
    client, db_session, auth_headers, query, hit, miss
):
    headers = await _headers(db_session, auth_headers)
    tok = _token()
    consumer = await UserFactory.create(db_session)
    sp_hit = await UserFactory.create(db_session, role="provider", name=f"{tok} {hit}")
    sp_miss = await UserFactory.create(db_session, role="provider", name=f"{tok} {miss}")
    mine = await _booking(db_session, consumer, sp_hit, BASE)
    await _booking(db_session, consumer, sp_miss, BASE)

    r = await client.get(URL, params={"provider_name": f"{tok} {query}"}, headers=headers)
    assert r.status_code == 200
    assert _ids(r.json()) == [str(mine.id)]
    assert r.json()["total"] == 1


async def test_filters_combine_and_total_matches(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    tok = _token()
    consumer = await UserFactory.create(db_session)
    other_consumer = await UserFactory.create(db_session)
    sp = await UserFactory.create(db_session, role="provider", name=f"{tok} Plumbing")
    other_sp = await UserFactory.create(db_session, role="provider", name="Other Plumbing")
    day = datetime(2099, 2, 14, 6, tzinfo=UTC)

    wanted = [
        await _booking(db_session, consumer, sp, day + timedelta(minutes=m)) for m in range(3)
    ]
    for b in wanted:
        await _dispute(db_session, b)
    # Each of these misses exactly one filter.
    undisputed = await _booking(db_session, consumer, sp, day)
    wrong_status = await _booking(db_session, consumer, sp, day, BookingStatus.CANCELLED)
    await _dispute(db_session, wrong_status)
    wrong_day = await _booking(db_session, consumer, sp, day + timedelta(days=1))
    await _dispute(db_session, wrong_day)
    wrong_phone = await _booking(db_session, other_consumer, sp, day)
    await _dispute(db_session, wrong_phone)
    wrong_sp = await _booking(db_session, consumer, other_sp, day)
    await _dispute(db_session, wrong_sp)
    # A second disputed payment on one booking must not duplicate it.
    await _dispute(db_session, wanted[0])

    params = {
        "status": "completed",
        "disputed": "true",
        "created_from": "2099-02-14",
        "created_to": "2099-02-14",
        "consumer_phone": consumer.phone,
        "provider_name": tok,
    }
    r = await client.get(URL, params=params, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert _ids(body) == [str(b.id) for b in reversed(wanted)]
    assert all(b["is_disputed"] for b in body["bookings"])
    assert str(undisputed.id) not in _ids(body)

    paged = await client.get(URL, params={**params, "per_page": 2, "page": 2}, headers=headers)
    assert paged.json()["total"] == 3
    assert _ids(paged.json()) == [str(wanted[0].id)]


@pytest.mark.parametrize("role", ["admin", "ops", "support"])
async def test_every_admin_role_can_list(client, db_session, auth_headers, role):
    headers = await _headers(db_session, auth_headers, role)
    r = await client.get(URL, params={"created_from": "2099-01-01"}, headers=headers)
    assert r.status_code == 200


async def test_consumer_is_403(client, db_session, auth_headers):
    consumer = await UserFactory.create(db_session)
    r = await client.get(URL, headers=auth_headers(str(consumer.id)))
    assert r.status_code == 403
