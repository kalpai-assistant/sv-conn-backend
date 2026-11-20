"""
SVC-484: "Notify me" alerts on a service tile that says "Coming soon" or
"No pros online right now" (Jira 14950 / 14952).

- Endpoints: ``GET / PUT / DELETE /users/me/service-alerts[/{service_id}]``.
  The server picks the kind from the tile's state at the point (served →
  ``no_pros_online``, not served → ``coming_soon``, listable → 409). One open
  alert per user + service; a second PUT moves it and restarts the 45 days.
- Sweep (``sweep_due_alerts``): ``coming_soon`` is due when served at the
  alert's point; ``no_pros_online`` when listable with an SP online for 10
  continuous minutes. Expired alerts stay silent, nothing goes out in quiet
  hours (22:00–08:00 IST), one push per customer, each alert fires once, and
  a customer with no token, an inactive one or a failed push leaves it open.
  2 statements per sweep.
- ``online_since`` is set on the off → on switch only.

The sweep runs inside the test's transaction (``run_sync``), so nothing is
committed. The task wrapper is run the same way, with its session and its
commit pointed at the test's.
"""

import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from firebase_admin import messaging as fcm_messaging
from sqlalchemy import event, select, update

from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service_alert import KIND_COMING_SOON, KIND_NO_PROS_ONLINE, ServiceAlert
from app.models.user import User
from app.services.account_service import AccountService
from app.services.from_price_service import CACHE_PREFIX, cache_key
from app.services.provider_service import ProviderService
from app.services.service_alert_service import (
    ALERT_LIFETIME,
    STEADY_ONLINE,
    listable_stage_names,
)
from app.tasks import service_alert_tasks
from app.tasks.service_alert_tasks import (
    forget_cached_tiles,
    in_quiet_hours,
    notify_service_alerts,
    sweep_due_alerts,
)
from tests.factories.user_factory import UserFactory
from tests.test_service_ranking import FAR, HERE, LAT, LNG, _service, _sp

URL = "/api/v1/users/me/service-alerts"
FROM_PRICES = "/api/v1/services/from-prices"
IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 11, 20, 12, 0, tzinfo=IST)  # outside quiet hours
FAR_LAT, FAR_LNG = 13.2, 77.9  # FAR, ~37 km from HERE
PUSH = "app.tasks.service_alert_tasks.send_push_notification"


@pytest.fixture(autouse=True)
async def _clean_cache(redis_client):
    keys = [k async for k in redis_client.scan_iter(f"{CACHE_PREFIX}:*")]
    if keys:
        await redis_client.delete(*keys)
    yield
    keys = [k async for k in redis_client.scan_iter(f"{CACHE_PREFIX}:*")]
    if keys:
        await redis_client.delete(*keys)


async def _customer(db, *, token=True, active=True) -> User:
    return await UserFactory.create(
        db,
        role="consumer",
        is_active=active,
        fcm_token=(f"cust-{uuid.uuid4().hex[:16]}" if token else None),
    )


@pytest.fixture
async def customer(db_session):
    return await _customer(db_session)


@pytest.fixture
def headers(customer, auth_headers):
    return auth_headers(str(customer.id), "consumer")


async def _put(client, headers, service_id, lat=LAT, lng=LNG):
    return await client.put(f"{URL}/{service_id}", json={"lat": lat, "lng": lng}, headers=headers)


async def _list(client, headers) -> list[dict]:
    resp = await client.get(URL, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows(db, user, service=None) -> list[ServiceAlert]:
    stmt = select(ServiceAlert).where(ServiceAlert.user_id == user.id)
    if service is not None:
        stmt = stmt.where(ServiceAlert.service_id == service.id)
    # populate_existing, not expire_all: refreshes these rows without turning
    # every other object's next attribute read into a lazy load.
    stmt = stmt.order_by(ServiceAlert.created_at).execution_options(populate_existing=True)
    return list((await db.scalars(stmt)).all())


async def _alert(
    db,
    user,
    service,
    *,
    kind=KIND_COMING_SOON,
    point=HERE,
    created_at=None,
    expires_at=None,
    notified_at=None,
    cancelled_at=None,
) -> ServiceAlert:
    created = created_at or NOON - timedelta(days=1)
    alert = ServiceAlert(
        user_id=user.id,
        service_id=service.id,
        location=point,
        kind=kind,
        created_at=created,
        expires_at=expires_at or created + ALERT_LIFETIME,
        notified_at=notified_at,
        cancelled_at=cancelled_at,
    )
    db.add(alert)
    await db.flush()
    return alert


async def _online_since(db, sp, since):
    await db.execute(
        update(ProviderProfile).where(ProviderProfile.user_id == sp.id).values(online_since=since)
    )
    await db.flush()


async def _sweep(db, now=NOON, *, limit=500, push_side_effect=None):
    """Run the sweep in the test's transaction. Returns (result, push mock)."""
    with patch(PUSH, side_effect=push_side_effect) as push:
        result = await db.run_sync(lambda s: sweep_due_alerts(s, now, limit=limit))
    return result, push


async def _notified_at(db, alert):
    return await db.scalar(select(ServiceAlert.notified_at).where(ServiceAlert.id == alert.id))


# --- endpoints ------------------------------------------------------------------


async def test_login_required(client):
    sid = uuid.uuid4()
    assert (await client.get(URL)).status_code == 401
    assert (await client.put(f"{URL}/{sid}", json={"lat": LAT, "lng": LNG})).status_code == 401
    assert (await client.delete(f"{URL}/{sid}")).status_code == 401


async def test_put_where_nobody_serves_is_coming_soon(client, db_session, headers, customer):
    service = await _service(db_session)
    resp = await _put(client, headers, service.id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["service_id"] == str(service.id)
    assert body["service_name"] == service.name
    assert body["kind"] == KIND_COMING_SOON
    created = datetime.fromisoformat(body["created_at"])
    assert datetime.fromisoformat(body["expires_at"]) - created == ALERT_LIFETIME
    assert await _list(client, headers) == [body]
    [row] = await _rows(db_session, customer)
    assert (row.notified_at, row.cancelled_at) == (None, None)


async def test_put_where_an_sp_serves_but_is_offline_is_no_pros_online(client, db_session, headers):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    resp = await _put(client, headers, service.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == KIND_NO_PROS_ONLINE


async def test_put_where_the_tile_shows_a_price_is_409(client, db_session, headers, customer):
    service = await _service(db_session)
    await _sp(db_session, service)
    resp = await _put(client, headers, service.id)
    assert resp.status_code == 409
    assert resp.json()["code"] == "SERVICE_ALREADY_AVAILABLE"
    assert await _rows(db_session, customer) == []


async def test_an_sp_out_of_area_doesnt_count(client, db_session, headers):
    service = await _service(db_session)
    await _sp(db_session, service)
    resp = await _put(client, headers, service.id, lat=FAR_LAT, lng=FAR_LNG)
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == KIND_COMING_SOON


@pytest.mark.parametrize(
    "setup",
    ["nobody", "offline", "online", "no_sp_token", "unverified", "unpriced", "far"],
)
async def test_the_kind_matches_the_tile(client, db_session, headers, setup):
    """The PUT reads the tile's own rules: price shown → 409, served → no pros
    online, not served → coming soon."""
    service = await _service(db_session)
    knobs = {
        "nobody": None,
        "offline": {"online": False},
        "online": {},
        "no_sp_token": {"has_token": False},
        "unverified": {"kyc": KYCStatus.PENDING},
        "unpriced": {"priced": False},
        "far": {"point": FAR},
    }[setup]
    if knobs is not None:
        await _sp(db_session, service, **knobs)
    tiles = (await client.get(FROM_PRICES, params={"lat": LAT, "lng": LNG}, headers=headers)).json()
    tile = next(t for t in tiles if t["service_id"] == str(service.id))
    resp = await _put(client, headers, service.id)
    if tile["from_price"] is not None:
        assert resp.status_code == 409
    elif tile["served"]:
        assert resp.json()["kind"] == KIND_NO_PROS_ONLINE
    else:
        assert resp.json()["kind"] == KIND_COMING_SOON


async def test_unknown_or_inactive_service_is_404(client, db_session, headers):
    inactive = await _service(db_session, active=False)
    for sid in (uuid.uuid4(), inactive.id):
        resp = await _put(client, headers, sid)
        assert resp.status_code == 404
        assert resp.json()["code"] == "SERVICE_NOT_FOUND"


async def test_coordinates_are_bounded(client, db_session, headers):
    service = await _service(db_session)
    assert (await _put(client, headers, service.id, lat=91)).status_code == 422
    assert (await _put(client, headers, service.id, lng=-181)).status_code == 422


async def test_a_second_put_moves_the_open_alert_and_restarts_expiry(
    client, db_session, headers, customer
):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)  # served near HERE only
    assert (await _put(client, headers, service.id)).json()["kind"] == KIND_NO_PROS_ONLINE
    long_ago = datetime.now(UTC) - timedelta(days=40)
    await db_session.execute(
        update(ServiceAlert)
        .where(ServiceAlert.user_id == customer.id)
        .values(created_at=long_ago, expires_at=long_ago + ALERT_LIFETIME)
    )

    moved = await _put(client, headers, service.id, lat=FAR_LAT, lng=FAR_LNG)
    assert moved.status_code == 200, moved.text
    assert moved.json()["kind"] == KIND_COMING_SOON  # re-decided at the new point
    [row] = await _rows(db_session, customer)
    assert row.created_at > long_ago + timedelta(days=39)
    assert row.expires_at - row.created_at == ALERT_LIFETIME
    from app.utils.geo import point_lat_lng

    assert point_lat_lng(row.location) == pytest.approx((FAR_LAT, FAR_LNG))


async def test_an_expired_alert_is_revived_by_a_put(client, db_session, headers, customer):
    service = await _service(db_session)
    await _alert(
        db_session,
        customer,
        service,
        created_at=datetime.now(UTC) - timedelta(days=50),
    )
    assert await _list(client, headers) == []  # expired: not listed
    assert (await _put(client, headers, service.id)).status_code == 200
    [row] = await _rows(db_session, customer)
    assert row.expires_at > datetime.now(UTC) + timedelta(days=44)


async def test_after_a_notify_or_cancel_a_put_opens_a_new_alert(
    client, db_session, headers, customer
):
    service = await _service(db_session)
    await _alert(db_session, customer, service, notified_at=NOON)
    other = await _service(db_session)
    await _alert(db_session, customer, other, cancelled_at=NOON)
    assert (await _put(client, headers, service.id)).status_code == 200
    assert (await _put(client, headers, other.id)).status_code == 200
    assert len(await _rows(db_session, customer, service)) == 2
    assert len(await _rows(db_session, customer, other)) == 2
    assert len(await _list(client, headers)) == 2


async def test_delete_cancels_and_is_idempotent(client, db_session, headers, customer):
    service = await _service(db_session)
    assert (await _put(client, headers, service.id)).status_code == 200
    resp = await client.delete(f"{URL}/{service.id}", headers=headers)
    assert resp.status_code == 204
    assert await _list(client, headers) == []
    [row] = await _rows(db_session, customer)
    assert row.cancelled_at is not None
    assert (await client.delete(f"{URL}/{service.id}", headers=headers)).status_code == 204
    assert (await client.delete(f"{URL}/{uuid.uuid4()}", headers=headers)).status_code == 204


async def test_get_lists_only_the_callers_open_unexpired_alerts_newest_first(
    client, db_session, headers, customer
):
    older, newer, done, gone, stale = [await _service(db_session) for _ in range(5)]
    now = datetime.now(UTC)
    await _alert(db_session, customer, older, created_at=now - timedelta(days=2))
    await _alert(
        db_session, customer, newer, kind=KIND_NO_PROS_ONLINE, created_at=now - timedelta(days=1)
    )
    await _alert(db_session, customer, done, notified_at=now)
    await _alert(db_session, customer, gone, cancelled_at=now)
    await _alert(db_session, customer, stale, created_at=now - timedelta(days=46))
    someone = await _customer(db_session)
    await _alert(db_session, someone, older)

    body = await _list(client, headers)
    assert [(a["service_id"], a["kind"]) for a in body] == [
        (str(newer.id), KIND_NO_PROS_ONLINE),
        (str(older.id), KIND_COMING_SOON),
    ]


async def test_one_open_alert_per_user_and_service_is_enforced_by_the_db(db_session, customer):
    from sqlalchemy.exc import IntegrityError

    service = await _service(db_session)
    await _alert(db_session, customer, service)
    await _alert(db_session, customer, service, notified_at=NOON)  # closed: fine
    with pytest.raises(IntegrityError):
        await _alert(db_session, customer, service)


# --- the sweep: when an alert is due --------------------------------------------


async def test_coming_soon_is_due_once_an_sp_serves_the_point_even_offline(db_session, customer):
    service = await _service(db_session)
    alert = await _alert(db_session, customer, service)
    result, push = await _sweep(db_session)
    assert push.call_count == 0 and result.notified == []

    await _sp(db_session, service, online=False)
    result, push = await _sweep(db_session)
    assert result.notified == [alert.id]
    assert await _notified_at(db_session, alert) == NOON
    push.assert_called_once()
    assert push.call_args.args[0] == customer.fcm_token
    assert push.call_args.kwargs["body"] == f"{service.name} is now available in your area"
    assert push.call_args.kwargs["data"] == {
        "type": "service_alert",
        "event": "service_available",
        "service_id": str(service.id),
        "service_name": service.name,
        "count": "1",
    }


async def test_coming_soon_is_not_due_for_an_sp_outside_the_point(db_session, customer):
    service = await _service(db_session)
    await _sp(db_session, service, point=FAR)
    await _alert(db_session, customer, service)
    result, push = await _sweep(db_session)
    assert result.notified == [] and push.call_count == 0


@pytest.mark.parametrize(
    ("online", "since", "due"),
    [
        pytest.param(True, NOON - STEADY_ONLINE, True, id="online_exactly_10_min"),
        pytest.param(True, NOON - STEADY_ONLINE + timedelta(seconds=1), False, id="9m59s"),
        pytest.param(True, None, False, id="online_since_null"),
        pytest.param(False, NOON - timedelta(hours=3), False, id="offline"),
    ],
)
async def test_no_pros_online_waits_for_10_continuous_minutes(
    db_session, customer, online, since, due
):
    service = await _service(db_session)
    sp = await _sp(db_session, service, online=online)
    await _online_since(db_session, sp, since)
    alert = await _alert(db_session, customer, service, kind=KIND_NO_PROS_ONLINE)
    result, push = await _sweep(db_session)
    assert (result.notified == [alert.id]) is due
    assert push.call_count == (1 if due else 0)


async def test_no_pros_online_still_needs_every_other_listable_stage(db_session, customer):
    """Only "online" is swapped: an SP online for an hour but without a push
    token isn't listable, so the alert waits."""
    assert "online" not in listable_stage_names(steady=True)
    assert set(listable_stage_names(steady=True)) | {"online"} == set(
        listable_stage_names(steady=False)
    )
    service = await _service(db_session)
    sp = await _sp(db_session, service, has_token=False)
    await _online_since(db_session, sp, NOON - timedelta(hours=1))
    await _alert(db_session, customer, service, kind=KIND_NO_PROS_ONLINE)
    result, _ = await _sweep(db_session)
    assert result.notified == []


async def test_coming_soon_does_not_wait_for_online(db_session, customer):
    """Served is enough for "Coming soon" (14952 #1): no online test at all."""
    service = await _service(db_session)
    await _sp(db_session, service, online=True)  # online_since NULL
    alert = await _alert(db_session, customer, service)
    result, _ = await _sweep(db_session)
    assert result.notified == [alert.id]


async def test_an_expired_alert_is_silent_and_untouched(db_session, customer):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    alert = await _alert(db_session, customer, service, expires_at=NOON - timedelta(seconds=1))
    result, push = await _sweep(db_session)
    assert result.notified == [] and push.call_count == 0
    row = (await _rows(db_session, customer))[0]
    assert (row.id, row.notified_at, row.cancelled_at) == (alert.id, None, None)


@pytest.mark.parametrize(
    ("hour", "minute", "sends"),
    [(21, 59, True), (22, 0, False), (7, 59, False), (8, 0, True)],
)
async def test_quiet_hours_send_nothing_and_read_nothing(db_session, customer, hour, minute, sends):
    now = datetime(2026, 11, 20, hour, minute, tzinfo=IST)
    assert in_quiet_hours(now) is not sends
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    alert = await _alert(db_session, customer, service, created_at=now - timedelta(days=1))
    statements, stop = _count_statements(db_session)
    try:
        result, push = await _sweep(db_session, now)
    finally:
        stop()
    assert push.call_count == (1 if sends else 0)
    assert (await _notified_at(db_session, alert) is not None) is sends
    if not sends:
        assert statements == []  # an overnight alert is simply re-read at 08:00


async def test_a_customer_with_no_token_waits(db_session):
    customer = await _customer(db_session, token=False)
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    alert = await _alert(db_session, customer, service)
    result, push = await _sweep(db_session)
    assert result.notified == [] and push.call_count == 0

    customer.fcm_token = "fresh-token"
    await db_session.flush()
    result, push = await _sweep(db_session)
    assert result.notified == [alert.id]


async def test_an_inactive_customer_or_service_is_skipped(db_session):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    gone = await _customer(db_session, active=False)
    await _alert(db_session, gone, service)
    live = await _customer(db_session)
    retired = await _service(db_session)
    await _sp(db_session, retired, online=False)
    await _alert(db_session, live, retired)
    retired.is_active = False
    await db_session.flush()
    result, push = await _sweep(db_session)
    assert result.notified == [] and push.call_count == 0


# --- the sweep: sending ---------------------------------------------------------


async def test_two_due_alerts_make_one_combined_push(db_session, customer):
    first, second = await _service(db_session), await _service(db_session)
    for service in (first, second):
        await _sp(db_session, service, online=False)
    a1 = await _alert(db_session, customer, first, created_at=NOON - timedelta(days=2))
    a2 = await _alert(db_session, customer, second, created_at=NOON - timedelta(days=1))
    result, push = await _sweep(db_session)
    assert sorted(result.notified) == sorted([a1.id, a2.id])
    push.assert_called_once()
    assert push.call_args.kwargs["body"] == "2 services you asked about are now available"
    data = push.call_args.kwargs["data"]
    assert (data["service_id"], data["count"]) == (str(first.id), "2")


async def test_each_alert_fires_once(db_session, customer):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    await _alert(db_session, customer, service)
    _, push = await _sweep(db_session)
    assert push.call_count == 1
    result, push = await _sweep(db_session, NOON + timedelta(minutes=2))
    assert result.notified == [] and push.call_count == 0


async def test_one_customers_failed_push_doesnt_stop_the_others(db_session):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    broken, fine = await _customer(db_session), await _customer(db_session)
    a_broken = await _alert(db_session, broken, service)
    a_fine = await _alert(db_session, fine, service)

    def send(token, **_):
        if token == broken.fcm_token:
            raise RuntimeError("FCM 503")
        return "ok"

    result, push = await _sweep(db_session, push_side_effect=send)
    assert push.call_count == 2
    assert result.notified == [a_fine.id]
    assert await _notified_at(db_session, a_broken) is None


async def test_a_dead_token_leaves_the_alert_open(db_session, customer):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    alert = await _alert(db_session, customer, service)

    def dead(token, **_):
        raise fcm_messaging.UnregisteredError("token expired")

    result, _ = await _sweep(db_session, push_side_effect=dead)
    assert result.notified == []
    assert [user.id for user, _ in result.dead_tokens] == [customer.id]
    assert await _notified_at(db_session, alert) is None


def _count_statements(db):
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", record)


async def test_a_sweep_is_two_statements_whatever_the_count(db_session):
    services = [await _service(db_session) for _ in range(2)]
    for service in services:
        for _ in range(2):
            await _sp(db_session, service, online=False)
    customers = [await _customer(db_session) for _ in range(3)]
    for customer in customers:
        for service in services:
            await _alert(db_session, customer, service)
    statements, stop = _count_statements(db_session)
    try:
        result, push = await _sweep(db_session)
    finally:
        stop()
    assert len(result.notified) == 6
    assert push.call_count == 3
    assert len(statements) == 2, statements
    assert "FOR UPDATE OF service_alerts SKIP LOCKED" in statements[0]
    assert statements[1].lstrip().startswith("UPDATE service_alerts")


async def test_the_batch_cuts_on_whole_customers(db_session):
    service, other = await _service(db_session), await _service(db_session)
    for s in (service, other):
        await _sp(db_session, s, online=False)
    first, second = sorted(
        [await _customer(db_session), await _customer(db_session)], key=lambda u: u.id
    )
    await _alert(db_session, first, service)
    for s in (service, other):  # second's two alerts straddle a limit of 2
        await _alert(db_session, second, s)

    result, push = await _sweep(db_session, limit=2)
    assert push.call_count == 1 and push.call_args.args[0] == first.fcm_token
    assert len(result.notified) == 1

    result, push = await _sweep(db_session, limit=2)
    assert push.call_count == 1 and push.call_args.kwargs["data"]["count"] == "2"
    assert len(result.notified) == 2


async def test_one_customer_above_the_limit_still_gets_the_first_batch(db_session, customer):
    services = [await _service(db_session) for _ in range(3)]
    for s in services:
        await _sp(db_session, s, online=False)
        await _alert(db_session, customer, s)
    result, push = await _sweep(db_session, limit=2)
    assert len(result.notified) == 2 and push.call_count == 1
    result, push = await _sweep(db_session, limit=2)
    assert len(result.notified) == 1


async def test_the_sweep_returns_each_notified_points_cache_key_once(db_session):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    for _ in range(2):  # two customers at the same point
        await _alert(db_session, await _customer(db_session), service)
    result, _ = await _sweep(db_session)
    assert result.cache_keys == [cache_key(LAT, LNG)]


# --- the task wrapper and the cache ---------------------------------------------


async def _run_task(db, monkeypatch, *, push_side_effect=None):
    """``notify_service_alerts`` against the test's own session, with its
    commits turned into flushes so nothing leaves the test's transaction."""
    monkeypatch.setattr(service_alert_tasks, "in_quiet_hours", lambda now: False)

    def run(session):
        import app.database

        monkeypatch.setattr(session, "commit", session.flush)
        monkeypatch.setattr(app.database, "SyncSessionLocal", lambda: nullcontext(session))
        with patch(PUSH, side_effect=push_side_effect) as push:
            notify_service_alerts()
        return push

    return await db.run_sync(run)


async def test_the_task_clears_a_dead_token_and_the_tile_cache(
    db_session, redis_client, monkeypatch
):
    service = await _service(db_session)
    await _sp(db_session, service, online=False)
    dead_customer, live_customer = await _customer(db_session), await _customer(db_session)
    dead_alert = await _alert(db_session, dead_customer, service)
    live_alert = await _alert(db_session, live_customer, service, point=FAR)
    await _sp(db_session, service, online=False, point=FAR)
    here_key, far_key = cache_key(LAT, LNG), cache_key(FAR_LAT, FAR_LNG)
    await redis_client.set(here_key, "[]")
    await redis_client.set(far_key, "[]")

    def send(token, **_):
        if token == dead_customer.fcm_token:
            raise fcm_messaging.UnregisteredError("token expired")
        return "ok"

    await _run_task(db_session, monkeypatch, push_side_effect=send)

    assert await _notified_at(db_session, live_alert) is not None
    assert await _notified_at(db_session, dead_alert) is None
    assert (
        await db_session.scalar(select(User.fcm_token).where(User.id == dead_customer.id)) is None
    )
    assert await redis_client.exists(far_key) == 0  # notified point: cleared
    assert await redis_client.exists(here_key) == 1  # nobody notified there


async def test_the_task_does_nothing_in_quiet_hours(monkeypatch):
    import app.database

    monkeypatch.setattr(service_alert_tasks, "in_quiet_hours", lambda now: True)

    def no_session():
        raise AssertionError("no session in quiet hours")

    monkeypatch.setattr(app.database, "SyncSessionLocal", no_session)
    notify_service_alerts()


def test_a_redis_error_is_logged_not_raised(caplog):
    from redis.exceptions import ConnectionError as RedisConnectionError

    with patch("redis.Redis.delete", side_effect=RedisConnectionError("down")):
        forget_cached_tiles([cache_key(LAT, LNG)])
    assert "service_alert.cache_clear_failed" in caplog.text


def test_no_keys_no_redis():
    with patch("redis.Redis.from_url") as from_url:
        forget_cached_tiles([])
    from_url.assert_not_called()


# --- online_since -----------------------------------------------------------------


async def _since(db, sp):
    return await db.scalar(
        select(ProviderProfile.online_since).where(ProviderProfile.user_id == sp.id)
    )


async def test_online_since_is_set_only_on_the_off_to_on_switch(db_session):
    service = await _service(db_session)
    sp = await _sp(db_session, service, online=False)
    providers = ProviderService(db_session)
    assert await _since(db_session, sp) is None

    before = datetime.now(UTC)
    await providers.set_online_status(sp.id, True)
    first = await _since(db_session, sp)
    assert first is not None and first >= before

    await providers.set_online_status(sp.id, True)  # on → on: unchanged
    assert await _since(db_session, sp) == first
    await providers.set_online_status(sp.id, False)  # on → off: unchanged
    assert await _since(db_session, sp) == first
    await providers.set_online_status(sp.id, False)  # off → off: unchanged
    assert await _since(db_session, sp) == first

    await providers.set_online_status(sp.id, True)  # off → on again: restarts
    assert await _since(db_session, sp) > first


# --- account deletion -------------------------------------------------------------


async def test_account_deletion_deletes_the_users_alerts(db_session, customer):
    service, other = await _service(db_session), await _service(db_session)
    await _alert(db_session, customer, service)
    await _alert(db_session, customer, other, notified_at=NOON)
    someone = await _customer(db_session)
    kept = await _alert(db_session, someone, service)
    await AccountService(db_session).delete_account(customer.id)
    assert await _rows(db_session, customer) == []
    assert [a.id for a in await _rows(db_session, someone)] == [kept.id]
