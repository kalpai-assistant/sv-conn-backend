"""SVC-486: scheduled commission changes (admin side, the apply, bookings).

- scheduling: the 14-day boundary for an increase (13 days refused, 14 fine), a
  decrease dated today applies at once, a later decrease waits, the value rules;
- one scheduled change per variant (409), a PATCH of the percent while one is
  scheduled (409) and a price-only PATCH (fine), cancel and its 409 / 404s;
- a PATCH decrease is recorded as an applied change and notified;
- bookings at 23:59:59 IST the day before and 00:00:00 on the date, with the
  clock pinned (``frozen``), and the change applying exactly once whether the
  sweep or a booking gets there first;
- a price edit between scheduling and applying: the new percent is taken of
  the price at apply time;
- the admin detail's ``pending_commission_change`` and the SP pricing screen's
  ``upcoming_commission``;
- the notices are enqueued only after commit, with the right kind.

The notice task itself, email, SMS and the ``/users/me`` email check are in
``tests/test_commission_change_notices.py``.

Other files commit rows, so every read is scoped to this test's own ids.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app import database
from app.models.booking_item import BookingItem
from app.models.commission_change import CommissionChange
from app.services import commission_changes as cc
from app.services.commission_changes import apply_due_commission_changes
from app.tasks import commission_tasks
from app.tasks.celery_app import celery_app
from tests.test_admin_catalog import _audit, _refused
from tests.test_admin_console_auth import _admin, _session
from tests.test_admin_variants import _book, _fresh, _money, _row, _service_with, _url, _variant

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 12, 6)
SCHEDULED = "COMMISSION_CHANGE_SCHEDULED"
NOT_SCHEDULED = "COMMISSION_CHANGE_NOT_SCHEDULED"


def _at(day: date, hour=0, minute=0, second=0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=IST)


@pytest.fixture
def frozen(monkeypatch):
    """``frozen(moment)`` pins the clock every SVC-486 rule reads."""

    def _pin(moment: datetime) -> None:
        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        monkeypatch.setattr(cc, "datetime", _Frozen)

    return _pin


@pytest.fixture(autouse=True)
def no_real_enqueue():
    """A test that commits must never reach the broker."""
    with patch.object(commission_tasks.send_commission_change_notices, "delay") as delay:
        yield delay


def _queued(db) -> list:
    return list(db.sync_session.info.get(database._AFTER_COMMIT, []))


def _changes_url(service, variant, suffix: str = "") -> str:
    return _url(service, variant, "/commission-changes" + suffix)


async def _schedule(client, admin, service, variant, new_pct, effective: date):
    return await client.post(
        _changes_url(service, variant),
        headers=_session(admin),
        json={"new_pct": str(new_pct), "effective_from": effective.isoformat()},
    )


async def _twenty_pct(db):
    """₹1000 at 20%: ₹200 commission, ₹800 payout."""
    service, (variant,) = await _service_with(db, ("1000.00", "200.00"), base="1000.00")
    return service, variant


async def _change(db, change_id) -> CommissionChange:
    return await db.scalar(
        select(CommissionChange)
        .where(CommissionChange.id == uuid.UUID(str(change_id)))
        .execution_options(populate_existing=True)
    )


async def _item_commission(db, booking) -> Decimal:
    return await db.scalar(
        select(BookingItem.platform_commission).where(BookingItem.booking_id == booking.id)
    )


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


async def test_an_increase_needs_14_days(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)

    r = await _schedule(client, admin, service, v, 25, DAY + timedelta(days=13))
    _refused(r, "effective_from")
    assert "2026-12-20" in r.json()["message"]

    r = await _schedule(client, admin, service, v, 25, DAY + timedelta(days=14))
    assert r.status_code == 201, r.text
    body = r.json()
    assert {k: body[k] for k in ("old_pct", "new_pct", "effective_from", "status")} == {
        "old_pct": 20.0,
        "new_pct": 25.0,
        "effective_from": "2026-12-20",
        "status": "scheduled",
    }
    assert body["applied_at"] is None
    # Nothing moves until the date.
    assert _money(await _fresh(db_session, v)) == (
        Decimal("1000.00"),
        Decimal("200.00"),
        Decimal("800.00"),
    )
    detail = await client.get(f"{_url(service)[: -len('/variants')]}", headers=_session(admin))
    assert _row(detail.json(), v.id)["pending_commission_change"] == {
        "id": body["id"],
        "new_pct": 25.0,
        "effective_from": "2026-12-20",
        "created_at": body["created_at"],
    }
    [row] = await _audit(db_session, admin)
    assert (row.action, row.target_type, row.target_id) == (
        "commission_change.schedule",
        "commission_change",
        body["id"],
    )
    assert row.before_json is None
    assert row.after_json["status"] == "scheduled"
    assert row.details["applied_now"] is False


async def test_a_decrease_dated_today_applies_at_once(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)

    r = await _schedule(client, admin, service, v, 15, DAY)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "applied"
    assert _money(await _fresh(db_session, v)) == (
        Decimal("1000.00"),
        Decimal("150.00"),
        Decimal("850.00"),
    )
    change = await _change(db_session, r.json()["id"])
    assert (
        change.old_platform_commission,
        change.old_provider_payout,
        change.new_platform_commission,
        change.new_provider_payout,
    ) == (Decimal("200.00"), Decimal("800.00"), Decimal("150.00"), Decimal("850.00"))
    assert change.applied_at is not None
    [row] = await _audit(db_session, admin)
    assert row.details["applied_now"] is True
    assert row.after_json["status"] == "applied"


async def test_a_later_decrease_waits_for_its_date(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)
    r = await _schedule(client, admin, service, v, 15, DAY + timedelta(days=1))
    assert (r.status_code, r.json()["status"]) == (201, "scheduled")
    assert _money(await _fresh(db_session, v))[1] == Decimal("200.00")


@pytest.mark.parametrize(
    ("new_pct", "effective", "field"),
    [
        ("15", DAY - timedelta(days=1), "effective_from"),  # in the past
        ("20", DAY + timedelta(days=30), "new_pct"),  # no change
        ("100", DAY + timedelta(days=30), "new_pct"),
        ("-1", DAY + timedelta(days=30), "new_pct"),
        ("12.345", DAY + timedelta(days=30), "new_pct"),
    ],
)
async def test_schedule_value_rules(client, db_session, frozen, new_pct, effective, field):
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)
    _refused(await _schedule(client, admin, service, v, new_pct, effective), field)
    assert await _audit(db_session, admin) == []


async def test_a_second_change_is_409_until_the_first_is_cancelled(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)
    first = await _schedule(client, admin, service, v, 25, DAY + timedelta(days=14))
    assert first.status_code == 201, first.text

    again = await _schedule(client, admin, service, v, 15, DAY + timedelta(days=2))
    assert again.status_code == 409, again.text
    assert again.json()["code"] == SCHEDULED
    assert again.json()["details"]["change_id"] == first.json()["id"]

    # A PATCH of the percent is refused too, a decrease included; a price isn't.
    for pct in ("15", "30"):
        r = await client.patch(
            _url(service, v), headers=_session(admin), json={"commission_pct": pct}
        )
        assert (r.status_code, r.json()["code"]) == (409, SCHEDULED)
    r = await client.patch(_url(service, v), headers=_session(admin), json={"base_price": "1100"})
    assert r.status_code == 200, r.text

    cancel = await client.post(
        _changes_url(service, v, f"/{first.json()['id']}/cancel"), headers=_session(admin)
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"
    assert cancel.json()["cancelled_at"] is not None
    r = await _schedule(client, admin, service, v, 15, DAY + timedelta(days=2))
    assert r.status_code == 201, r.text


async def test_cancel_rules(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)
    other_service, other = await _twenty_pct(db_session)
    change = (await _schedule(client, admin, service, v, 25, DAY + timedelta(days=14))).json()
    url = _changes_url(service, v, f"/{change['id']}/cancel")

    # Another variant's change, and an unknown id: 404.
    for wrong in (
        _changes_url(other_service, other, f"/{change['id']}/cancel"),
        _changes_url(service, v, f"/{uuid.uuid4()}/cancel"),
    ):
        r = await client.post(wrong, headers=_session(admin))
        assert (r.status_code, r.json()["code"]) == (404, "COMMISSION_CHANGE_NOT_FOUND")

    assert (await client.post(url, headers=_session(admin))).status_code == 200
    again = await client.post(url, headers=_session(admin))
    assert (again.status_code, again.json()["code"]) == (409, NOT_SCHEDULED)
    assert again.json()["details"] == {"status": "cancelled"}
    rows = [r for r in await _audit(db_session, admin) if r.action == "commission_change.cancel"]
    assert len(rows) == 1
    assert (rows[0].before_json["status"], rows[0].after_json["status"]) == (
        "scheduled",
        "cancelled",
    )
    # The variant never moved.
    assert _money(await _fresh(db_session, v))[1] == Decimal("200.00")


async def test_a_due_change_cant_be_cancelled_it_has_applied(client, db_session, frozen):
    """The cancel applies what is due first, so the boundary holds even if the
    sweep hasn't run yet."""
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)
    change = (await _schedule(client, admin, service, v, 25, DAY + timedelta(days=14))).json()
    frozen(_at(DAY + timedelta(days=14)))
    r = await client.post(
        _changes_url(service, v, f"/{change['id']}/cancel"), headers=_session(admin)
    )
    assert (r.status_code, r.json()["details"]) == (409, {"status": "applied"})
    assert _money(await _fresh(db_session, v))[1] == Decimal("250.00")


async def test_a_patch_decrease_is_recorded_as_an_applied_change(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)
    r = await client.patch(_url(service, v), headers=_session(admin), json={"commission_pct": "15"})
    assert r.status_code == 200, r.text
    [row] = await _audit(db_session, admin)
    change = await _change(db_session, row.details["commission_change_id"])
    assert (change.status, change.effective_from, change.old_pct, change.new_pct) == (
        "applied",
        DAY,
        Decimal("20.00"),
        Decimal("15.00"),
    )
    assert (change.new_platform_commission, change.new_provider_payout) == (
        Decimal("150.00"),
        Decimal("850.00"),
    )
    assert change.created_by == admin.id


# ---------------------------------------------------------------------------
# applying, and bookings at the boundary
# ---------------------------------------------------------------------------


async def test_bookings_either_side_of_midnight_ist(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY - timedelta(days=14), 10))
    service, v = await _twenty_pct(db_session)
    change_id = (await _schedule(client, admin, service, v, 25, DAY)).json()["id"]

    frozen(_at(DAY - timedelta(days=1), 23, 59, 59))
    before = await _book(db_session, service, v)
    assert await _item_commission(db_session, before) == Decimal("200.00")
    assert before.platform_commission == Decimal("200.00")
    assert (await _change(db_session, change_id)).status == "scheduled"

    frozen(_at(DAY, 0, 0, 0))
    on_the_day = await _book(db_session, service, v)
    assert await _item_commission(db_session, on_the_day) == Decimal("250.00")
    change = await _change(db_session, change_id)
    assert change.status == "applied"
    applied_at = change.applied_at

    # The booking before the date kept its split; later ones don't re-apply.
    assert await _item_commission(db_session, before) == Decimal("200.00")
    again = await _book(db_session, service, v)
    assert await _item_commission(db_session, again) == Decimal("250.00")
    assert (await _change(db_session, change_id)).applied_at == applied_at
    assert await apply_due_commission_changes(db_session, service_ids=[service.id]) == []


async def test_the_sweep_applies_once_and_a_booking_after_it_doesnt(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY - timedelta(days=14), 10))
    service, v = await _twenty_pct(db_session)
    change_id = uuid.UUID((await _schedule(client, admin, service, v, 25, DAY)).json()["id"])

    frozen(_at(DAY, 0, 0, 30))
    assert change_id in await apply_due_commission_changes(db_session)
    assert change_id not in await apply_due_commission_changes(db_session)
    assert await apply_due_commission_changes(db_session, service_ids=[service.id]) == []
    assert _money(await _fresh(db_session, v))[1] == Decimal("250.00")
    booking = await _book(db_session, service, v)
    assert await _item_commission(db_session, booking) == Decimal("250.00")
    rows = (
        await db_session.scalars(
            select(CommissionChange).where(CommissionChange.service_variant_id == v.id)
        )
    ).all()
    assert [(r.status, r.new_platform_commission) for r in rows] == [("applied", Decimal("250.00"))]


async def test_a_cancelled_or_future_change_is_never_applied(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY - timedelta(days=14), 10))
    service, v = await _twenty_pct(db_session)
    _, later = await _service_with(db_session, ("1000.00", "200.00"), base="1000.00")
    first = (await _schedule(client, admin, service, v, 25, DAY)).json()
    await client.post(_changes_url(service, v, f"/{first['id']}/cancel"), headers=_session(admin))

    frozen(_at(DAY, 1))
    assert await apply_due_commission_changes(db_session, service_ids=[service.id]) == []
    assert _money(await _fresh(db_session, v))[1] == Decimal("200.00")
    assert await apply_due_commission_changes(db_session, service_ids=[]) == []


async def test_a_price_edit_before_the_date_is_split_at_the_new_pct(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY - timedelta(days=14), 10))
    service, v = await _twenty_pct(db_session)
    await _schedule(client, admin, service, v, 25, DAY)
    r = await client.patch(_url(service, v), headers=_session(admin), json={"base_price": "1200"})
    assert r.status_code == 200, r.text
    # The price edit keeps today's ratio.
    assert _money(await _fresh(db_session, v)) == (
        Decimal("1200.00"),
        Decimal("240.00"),
        Decimal("960.00"),
    )

    frozen(_at(DAY, 0, 0, 0))
    booking = await _book(db_session, service, v)
    assert await _item_commission(db_session, booking) == Decimal("300.00")
    assert _money(await _fresh(db_session, v)) == (
        Decimal("1200.00"),
        Decimal("300.00"),
        Decimal("900.00"),
    )


async def test_an_admin_edit_on_the_day_applies_the_due_change_first(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY - timedelta(days=14), 10))
    service, v = await _twenty_pct(db_session)
    await _schedule(client, admin, service, v, 25, DAY)
    frozen(_at(DAY, 0, 0, 5))
    r = await client.patch(_url(service, v), headers=_session(admin), json={"label": "Renamed"})
    assert r.status_code == 200, r.text
    assert _row(r.json(), v.id)["commission_pct"] == 25.0
    assert _row(r.json(), v.id)["pending_commission_change"] is None


def test_the_sweep_runs_every_minute():
    entry = celery_app.conf.beat_schedule["apply-commission-changes"]
    assert entry == {
        "task": "app.tasks.commission_tasks.apply_commission_changes",
        "schedule": 60.0,
    }


def test_the_sweep_applies_one_service_per_transaction():
    """Never two services' locks at once. A service that fails is rolled back
    and logged, and the rest still apply."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    first, broken, last = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    applied = [uuid.uuid4(), uuid.uuid4()]
    apply = AsyncMock(side_effect=[[applied[0]], RuntimeError("deadlock detected"), [applied[1]]])
    with (
        patch("app.database.AsyncSessionLocal", factory),
        patch(
            "app.services.commission_changes.due_service_ids",
            AsyncMock(return_value=[first, broken, last]),
        ),
        patch("app.services.commission_changes.apply_due_commission_changes", apply),
    ):
        assert commission_tasks.apply_commission_changes() == 2
    assert [c.kwargs["service_ids"] for c in apply.await_args_list] == [[first], [broken], [last]]
    assert session.commit.await_count == 2
    assert session.rollback.await_count == 1


async def test_due_service_ids_lists_each_due_service_once(client, db_session, frozen):
    admin = await _admin(db_session)
    frozen(_at(DAY - timedelta(days=14), 10))
    service, v = await _twenty_pct(db_session)
    second = await _variant(db_session, service, "500.00", "100.00", sort_order=1)
    await _schedule(client, admin, service, v, 25, DAY)
    await _schedule(client, admin, service, second, 25, DAY)
    frozen(_at(DAY - timedelta(days=1), 23, 59, 59))
    assert service.id not in await cc.due_service_ids(db_session)
    frozen(_at(DAY))
    due = await cc.due_service_ids(db_session)
    assert due.count(service.id) == 1


# ---------------------------------------------------------------------------
# the notices are enqueued after commit
# ---------------------------------------------------------------------------


async def test_schedule_cancel_and_patch_decrease_enqueue_after_commit(
    client, db_session, frozen, no_real_enqueue
):
    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, v = await _twenty_pct(db_session)
    _, other = await _service_with(db_session, ("1000.00", "200.00"), base="1000.00")

    change = (await _schedule(client, admin, service, v, 25, DAY + timedelta(days=14))).json()
    await client.post(_changes_url(service, v, f"/{change['id']}/cancel"), headers=_session(admin))
    await client.patch(_url(service, v), headers=_session(admin), json={"commission_pct": "15"})
    # A refused write queues nothing.
    await _schedule(client, admin, service, v, 30, DAY + timedelta(days=1))

    # Each admin request commits its session slide, which runs the previous
    # request's callbacks; run whatever is still waiting, as the last commit would.
    for fn in _queued(db_session):
        fn()
    [patched] = [
        r for r in await _audit(db_session, admin) if r.details.get("commission_change_id")
    ]
    assert [c.kwargs for c in no_real_enqueue.call_args_list] == [
        {"change_id": change["id"], "kind": "scheduled"},
        {"change_id": change["id"], "kind": "cancelled"},
        {"change_id": patched.details["commission_change_id"], "kind": "scheduled"},
    ]


# ---------------------------------------------------------------------------
# the SP pricing screen
# ---------------------------------------------------------------------------


async def test_the_pricing_screen_shows_the_upcoming_commission(
    client, db_session, auth_headers, frozen
):
    from tests.test_sp_pricing_api import PRICING, _set, _sp
    from tests.test_sp_pricing_api import _service as _pricing_service

    admin = await _admin(db_session)
    frozen(_at(DAY, 10))
    service, variant = await _pricing_service(db_session)
    sp = await _sp(db_session, service)
    saved = await _set(client, auth_headers, sp, variant, price_amount="2600")
    assert saved["upcoming_commission"] is None

    r = await _schedule(client, admin, service, variant, 35, DAY + timedelta(days=14))
    assert r.status_code == 201, r.text
    resp = await client.get(PRICING, headers=auth_headers(str(sp.id), "provider"))
    assert resp.status_code == 200, resp.text
    [group] = [g for g in resp.json()["services"] if g["service_id"] == str(service.id)]
    assert group["variants"][0]["upcoming_commission"] == {
        "new_pct": 35.0,
        "effective_from": "2026-12-20",
    }
    saved = await _set(client, auth_headers, sp, variant, price_amount="2700")
    assert saved["upcoming_commission"] == {"new_pct": 35.0, "effective_from": "2026-12-20"}
