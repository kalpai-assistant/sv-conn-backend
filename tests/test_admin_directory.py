"""
SVC-427: the admin SP + Pro directories.

- ``GET /admin/providers``: ``is_active`` / ``kyc_status`` filters, shape unchanged;
- ``GET /admin/providers/{id}``: service area, rating, services, and the bookings
  created in the last 30 IST days (clock pinned at the boundary);
- ``GET /admin/pros``: filters (KYC as shown), search, pagination and ``total``;
- ``GET /admin/pros/{id}``: rating (null with no reviews) and completed jobs;
- ``GET /admin/bookings``: ``provider_id`` / ``professional_id``;
- roles: ops edits the team (audited as ops), never deactivates; support reads.

Endpoint commits are real in this harness, so every list read is scoped to this
test's own rows (an owner id or a unique search term).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.admin_action import AdminAction
from app.models.booking import BookingStatus
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.review import Review
from app.models.service import Service
from app.models.user import AdminRole
from app.services import admin_team_service
from app.services.admin_auth_service import provision_admin
from tests.test_accept_contract import _service
from tests.test_admin_console_auth import PASSWORD, _session
from tests.test_admin_team import _job, _owner
from tests.test_two_level_dispatch import _team_pro

A = "/api/v1/admin"
IST = timezone(timedelta(hours=5, minutes=30))
# A Monday morning in IST; 30 IST days back, today included, starts on 11 Oct.
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=IST)
SINCE = "2026-10-11"

PROVIDER_ROW_FIELDS = {
    "user_id",
    "name",
    "phone",
    "business_name",
    "kyc_status",
    "is_online",
    "user_is_active",
    "active_pro_count",
    "created_at",
}


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin the clock the 30-day window reads (as in test_provider_earnings_by_pro)."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(admin_team_service, "datetime", _Frozen)
    return NOW


async def _console(db, role: AdminRole):
    user = await provision_admin(db, f"d{uuid.uuid4().hex[:10]}@kalpai.in", role, PASSWORD)
    await db.commit()
    return user


@pytest.fixture
async def admin(db_session):
    return await _console(db_session, AdminRole.ADMIN)


@pytest.fixture
async def ops(db_session):
    return await _console(db_session, AdminRole.OPS)


@pytest.fixture
async def support(db_session):
    return await _console(db_session, AdminRole.SUPPORT)


def _tag() -> str:
    return f"Dir{uuid.uuid4().hex[:8]}"


async def _profile(db, sp) -> ProviderProfile:
    return await db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == sp.id))


# --- GET /admin/providers ------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_list_filters_by_account_and_kyc(client, db_session, admin):
    tag = _tag()
    service = await _service(db_session)
    live = await _owner(db_session, service, business=f"{tag} Live")
    pending = await _owner(db_session, service, business=f"{tag} Pending")
    (await _profile(db_session, pending)).kyc_status = KYCStatus.PENDING
    gone = await _owner(db_session, service, business=f"{tag} Gone")
    gone.is_active = False
    await db_session.commit()

    async def ids(**params):
        r = await client.get(f"{A}/providers", params={"q": tag, **params}, headers=_session(admin))
        assert r.status_code == 200, r.text
        return {row["user_id"] for row in r.json()}

    assert await ids() == {str(live.id), str(pending.id), str(gone.id)}
    assert await ids(is_active="false") == {str(gone.id)}
    assert await ids(is_active="true") == {str(live.id), str(pending.id)}
    assert await ids(kyc_status="pending") == {str(pending.id)}
    assert await ids(kyc_status="verified", is_active="true") == {str(live.id)}
    assert await ids(kyc_status="rejected") == set()

    # Still the bare list of the same rows: the old Teams screen reads it.
    r = await client.get(f"{A}/providers", params={"q": tag}, headers=_session(admin))
    assert isinstance(r.json(), list)
    assert all(set(row) == PROVIDER_ROW_FIELDS for row in r.json())


# --- GET /admin/providers/{id} -------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_detail_adds_area_rating_and_services(client, db_session, admin):
    service = await _service(db_session)
    sp = await _owner(db_session, service)
    profile = await _profile(db_session, sp)
    profile.avg_rating = 4.25
    profile.total_completed_jobs = 7
    retired = Service(
        category_id=service.category_id,
        name="Aaa Retired",
        base_price=100.0,
        duration_minutes=30,
        is_active=False,
    )
    db_session.add(retired)
    await db_session.flush()
    db_session.add(ProviderServiceModel(provider_id=sp.id, service_id=retired.id))
    await db_session.commit()

    r = await client.get(f"{A}/providers/{sp.id}", headers=_session(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    # Every field of the row is still there, unchanged.
    assert PROVIDER_ROW_FIELDS <= set(body)
    assert body["user_id"] == str(sp.id)
    assert body["service_area"] == {"lat": pytest.approx(12.97), "lng": 77.62, "radius_km": 10}
    assert body["avg_rating"] == 4.25
    assert body["total_completed_jobs"] == 7
    assert body["services"] == [
        {"id": str(retired.id), "name": "Aaa Retired", "is_active": False},
        {"id": str(service.id), "name": service.name, "is_active": True},
    ]


@pytest.mark.asyncio
async def test_provider_detail_service_area_is_null_without_a_centre(client, db_session, admin):
    sp = await _owner(db_session)
    (await _profile(db_session, sp)).service_area_center = None
    await db_session.commit()

    body = (await client.get(f"{A}/providers/{sp.id}", headers=_session(admin))).json()
    assert body["service_area"] is None
    assert body["bookings_30d"]["total"] == 0
    assert set(body["bookings_30d"]["by_status"].values()) == {0}


@pytest.mark.asyncio
async def test_booking_volume_counts_bookings_created_in_the_last_30_ist_days(
    client, db_session, admin, frozen_now
):
    service = await _service(db_session)
    sp = await _owner(db_session, service)
    other = await _owner(db_session, service)
    pro = await _team_pro(db_session, sp.id)

    async def booked(at, status, provider=sp):
        job = await _job(db_session, service, provider, pro, status)
        job.created_at = at
        await db_session.flush()
        return job

    # 23:59 IST on 10 Oct is the last minute before the window; 00:00 on 11 Oct the first.
    await booked(datetime(2026, 10, 10, 23, 59, tzinfo=IST), BookingStatus.COMPLETED)
    await booked(datetime(2026, 10, 11, 0, 0, tzinfo=IST), BookingStatus.COMPLETED)
    await booked(datetime(2026, 10, 20, 12, 0, tzinfo=IST), BookingStatus.CANCELLED)
    await booked(datetime(2026, 11, 9, 9, 0, tzinfo=IST), BookingStatus.ACCEPTED)
    # Someone else's booking never counts.
    await booked(datetime(2026, 11, 1, 9, 0, tzinfo=IST), BookingStatus.COMPLETED, other)
    await db_session.commit()

    volume = (await client.get(f"{A}/providers/{sp.id}", headers=_session(admin))).json()[
        "bookings_30d"
    ]
    assert volume["since"] == SINCE
    assert volume["total"] == 3
    assert volume["by_status"] == {
        "requested": 0,
        "accepted": 1,
        "en_route": 0,
        "in_progress": 0,
        "completed": 1,
        "cancelled": 1,
        "expired": 0,
    }

    # The "last 30 days" link lists exactly the rows counted.
    r = await client.get(
        f"{A}/bookings",
        params={"provider_id": str(sp.id), "created_from": volume["since"]},
        headers=_session(admin),
    )
    assert r.json()["total"] == volume["total"]


# --- GET /admin/pros -----------------------------------------------------------------


async def _directory_team(db):
    """An SP (KYC verified) with its Pro-for-self and three team Pros: one
    online and verified, one removed, one offline with KYC pending."""
    service = await _service(db)
    sp = await _owner(db, service, business=f"{_tag()} Works")
    tag = _tag()
    online = await _team_pro(db, sp.id, name=f"{tag} Rohan", online=True)
    removed = await _team_pro(db, sp.id, name=f"{tag} Gone", active=False, online=False)
    pending = await _team_pro(db, sp.id, name=f"{tag} Pending", online=False, kyc=KYCStatus.PENDING)
    await db.commit()
    return sp, tag, online, removed, pending


async def _pros(client, user, **params):
    r = await client.get(f"{A}/pros", params=params, headers=_session(user))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_pro_list_rows_name_their_owner(client, db_session, admin):
    sp, _, online, *_ = await _directory_team(db_session)
    body = await _pros(client, admin, sp_user_id=str(sp.id))

    assert body["total"] == 4
    assert body["page"] == 1
    assert body["per_page"] == 50
    row = next(item for item in body["items"] if item["id"] == str(online.id))
    assert row["service_provider"] == {
        "user_id": str(sp.id),
        "name": sp.name,
        "phone": sp.phone,
        "business_name": (await _profile(db_session, sp)).business_name,
    }
    assert row["is_self"] is False
    assert row["is_online"] is True


@pytest.mark.asyncio
async def test_pro_list_filters(client, db_session, admin):
    sp, _, online, removed, pending = await _directory_team(db_session)
    self_pro = next(
        item["id"]
        for item in (await _pros(client, admin, sp_user_id=str(sp.id)))["items"]
        if item["is_self"]
    )

    async def ids(**params):
        body = await _pros(client, admin, sp_user_id=str(sp.id), **params)
        assert body["total"] == len(body["items"])
        return {item["id"] for item in body["items"]}

    assert await ids(is_active="false") == {str(removed.id)}
    assert str(removed.id) not in await ids(is_active="true")
    assert await ids(is_online="true") >= {str(online.id)}
    assert str(online.id) not in await ids(is_online="false")
    # KYC as each row shows it: the Pro-for-self shows its verified owner's KYC,
    # whatever its own column says, so "pending" doesn't list it.
    assert await ids(kyc_status="pending") == {str(pending.id)}
    assert await ids(kyc_status="verified") == {self_pro, str(online.id), str(removed.id)}


@pytest.mark.asyncio
async def test_pro_list_search_by_name_and_phone(client, db_session, admin):
    _, tag, online, removed, pending = await _directory_team(db_session)
    pending_user_phone = (await _pros(client, admin, q=f"{tag} Pending"))["items"][0]["phone"]

    assert {i["id"] for i in (await _pros(client, admin, q=tag))["items"]} == {
        str(online.id),
        str(removed.id),
        str(pending.id),
    }
    assert {i["id"] for i in (await _pros(client, admin, q=tag.lower()))["items"]} == {
        str(online.id),
        str(removed.id),
        str(pending.id),
    }
    spaced = f"{pending_user_phone[3:8]} {pending_user_phone[8:]}"
    assert str(pending.id) in {i["id"] for i in (await _pros(client, admin, q=spaced))["items"]}
    # A wildcard matches itself, not everything.
    assert (await _pros(client, admin, q=f"{tag}%"))["total"] == 0


@pytest.mark.asyncio
async def test_pro_list_pages_walk_every_row_once(client, db_session, admin):
    sp, *_ = await _directory_team(db_session)
    pages = [
        await _pros(client, admin, sp_user_id=str(sp.id), page=page, per_page=3) for page in (1, 2)
    ]
    assert [len(p["items"]) for p in pages] == [3, 1]
    assert {p["total"] for p in pages} == {4}
    ids = [item["id"] for p in pages for item in p["items"]]
    assert len(set(ids)) == 4


# --- GET /admin/pros/{id} ------------------------------------------------------------


@pytest.mark.asyncio
async def test_pro_detail_rating_and_completed_jobs(client, db_session, admin):
    service = await _service(db_session)
    sp = await _owner(db_session, service)
    rated = await _team_pro(db_session, sp.id, name="Rated")
    unrated = await _team_pro(db_session, sp.id, name="Unrated")
    for rating in (5, 4, 4):
        job = await _job(db_session, service, sp, rated, BookingStatus.COMPLETED)
        db_session.add(
            Review(
                booking_id=job.id,
                reviewer_id=job.consumer_id,
                reviewee_id=sp.id,
                professional_id=rated.id,
                rating=rating,
            )
        )
    await _job(db_session, service, sp, rated, BookingStatus.CANCELLED)
    await _job(db_session, service, sp, unrated, BookingStatus.COMPLETED)
    await db_session.commit()

    body = (await client.get(f"{A}/pros/{rated.id}", headers=_session(admin))).json()
    assert body["rating_avg"] == 4.3
    assert body["rating_count"] == 3
    assert body["completed_job_count"] == 3
    assert body["service_provider"]["user_id"] == str(sp.id)

    body = (await client.get(f"{A}/pros/{unrated.id}", headers=_session(admin))).json()
    assert body["rating_avg"] is None
    assert body["rating_count"] == 0
    assert body["completed_job_count"] == 1


# --- GET /admin/bookings -------------------------------------------------------------


@pytest.mark.asyncio
async def test_bookings_filter_by_provider_and_professional(client, db_session, admin):
    service = await _service(db_session)
    sp = await _owner(db_session, service)
    other = await _owner(db_session, service)
    pro = await _team_pro(db_session, sp.id)
    other_pro = await _team_pro(db_session, sp.id)
    mine = await _job(db_session, service, sp, pro, BookingStatus.COMPLETED)
    teammate = await _job(db_session, service, sp, other_pro, BookingStatus.ACCEPTED)
    elsewhere = await _job(db_session, service, other, other_pro, BookingStatus.COMPLETED)
    await db_session.commit()

    async def ids(**params):
        r = await client.get(f"{A}/bookings", params=params, headers=_session(admin))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == len(body["bookings"])
        return {b["id"] for b in body["bookings"]}

    assert await ids(provider_id=str(sp.id)) == {str(mine.id), str(teammate.id)}
    assert await ids(professional_id=str(pro.id)) == {str(mine.id)}
    assert await ids(professional_id=str(other_pro.id)) == {str(teammate.id), str(elsewhere.id)}
    assert await ids(provider_id=str(other.id), professional_id=str(pro.id)) == set()
    assert await ids(provider_id=str(sp.id), status="accepted") == {str(teammate.id)}


# --- Roles ---------------------------------------------------------------------------


async def _team_actions(db, actor, action):
    return (
        (
            await db.execute(
                select(AdminAction).where(
                    AdminAction.admin_user_id == actor.id, AdminAction.action == action
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_ops_reassigns_and_the_audit_row_names_ops(client, db_session, ops):
    service = await _service(db_session)
    source = await _owner(db_session, service)
    target = await _owner(db_session, service)
    pro = await _team_pro(db_session, source.id)
    await db_session.commit()

    r = await client.post(
        f"{A}/pros/{pro.id}/reassign",
        json={"target_sp_user_id": str(target.id), "reason": "Moved by ops"},
        headers=_session(ops),
    )
    assert r.status_code == 200, r.text
    [row] = await _team_actions(db_session, ops, "pro_reassigned")
    assert row.target_id == str(pro.id)
    assert row.sp_user_id == source.id
    assert row.to_sp_user_id == target.id
    assert row.reason == "Moved by ops"


@pytest.mark.asyncio
async def test_ops_force_removes_and_the_audit_row_names_ops(client, db_session, ops):
    sp = await _owner(db_session)
    pro = await _team_pro(db_session, sp.id)
    await db_session.commit()

    r = await client.post(
        f"{A}/pros/{pro.id}/force-remove", json={"reason": "Left"}, headers=_session(ops)
    )
    assert r.status_code == 200, r.text
    [row] = await _team_actions(db_session, ops, "pro_force_removed")
    assert row.target_id == str(pro.id)


@pytest.mark.asyncio
async def test_ops_cannot_deactivate_a_user(client, db_session, ops):
    sp = await _owner(db_session)
    await db_session.commit()

    r = await client.patch(f"{A}/users/{sp.id}/deactivate", json={}, headers=_session(ops))
    assert r.status_code == 403
    await db_session.refresh(sp)
    assert sp.is_active is True


@pytest.mark.asyncio
async def test_support_reads_the_directories_and_history_but_cannot_edit(
    client, db_session, support
):
    service = await _service(db_session)
    sp = await _owner(db_session, service)
    target = await _owner(db_session, service)
    pro = await _team_pro(db_session, sp.id)
    await db_session.commit()
    headers = _session(support)

    for path in (
        "/providers",
        f"/providers/{sp.id}",
        "/pros",
        f"/pros/{pro.id}",
        f"/actions?sp_user_id={sp.id}",
    ):
        r = await client.get(f"{A}{path}", headers=headers)
        assert r.status_code == 200, f"{path}: {r.status_code}"

    writes = [
        client.post(f"{A}/pros/{pro.id}/force-remove", json={"reason": "xyz"}, headers=headers),
        client.post(
            f"{A}/pros/{pro.id}/reassign",
            json={"target_sp_user_id": str(target.id), "reason": "xyz"},
            headers=headers,
        ),
        client.patch(f"{A}/users/{sp.id}/deactivate", json={}, headers=headers),
    ]
    for call in writes:
        assert (await call).status_code == 403
    await db_session.refresh(pro)
    assert pro.is_active is True
