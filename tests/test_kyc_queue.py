"""
SVC-424: the admin KYC queue's count route, status=all, kyc_status on each row,
the sort orders, and what is left out (deleted accounts, removed Pros).

Handlers that commit leak rows across tests here, so every check looks at this
test's own ids, and a count is only ever compared with the queue's full list.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import AdminRole, User, UserRole
from app.services.account_service import AccountService
from app.services.admin_auth_service import provision_admin
from app.services.team_service import TeamService
from app.utils import sms_client
from app.utils.security import create_admin_session_token
from tests.factories.user_factory import UserFactory, _random_phone
from tests.test_provider_team import TEAM, _accept, _invite, _sp

QUEUE = "/api/v1/admin/providers/kyc/pending"
COUNT = f"{QUEUE}/count"

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
ALL_STATUSES = ("submitted", "verified", "rejected", "pending", "auto_approved", "all")
ALL_KINDS = ("sp", "pro", "all")


def _at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


async def _bot(db) -> User:
    """The AI verifier's user. A migration creates it, so create_all has none."""
    bot_id = uuid.UUID(get_settings().KYC_VERIFIER_BOT_USER_ID)
    bot = await db.get(User, bot_id)
    if bot is None:
        bot = User(id=bot_id, phone=_random_phone(), name="KYC bot", role=UserRole.CONSUMER)
        db.add(bot)
        await db.flush()
    return bot


async def _sp_at(db, kyc: KYCStatus, **fields) -> tuple[User, ProviderProfile]:
    user = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(user_id=user.id, kyc_status=kyc, **fields)
    db.add(profile)
    await db.flush()
    return user, profile


async def _pro_at(db, owner: User, kyc: KYCStatus, **fields) -> tuple[User, Professional]:
    user = await UserFactory.create(db, role="consumer")
    pro = Professional(
        service_provider_id=owner.id,
        user_id=user.id,
        display_name="Rohan",
        kyc_status=kyc,
        **fields,
    )
    db.add(pro)
    await db.flush()
    return user, pro


async def _admin_headers(db, auth_headers) -> dict:
    admin = await UserFactory.create(db, role="admin")
    return auth_headers(str(admin.id), "admin")


async def _queue(client, headers, **params) -> list[dict]:
    """Every row, walking the pages."""
    rows: list[dict] = []
    page = 1
    while True:
        r = await client.get(
            QUEUE, params={**params, "page": page, "per_page": 200}, headers=headers
        )
        assert r.status_code == 200, r.text
        if not r.json():
            return rows
        rows += r.json()
        page += 1


async def _count(client, headers, **params) -> int:
    r = await client.get(COUNT, params=params, headers=headers)
    assert r.status_code == 200, r.text
    assert list(r.json()) == ["count"], r.text
    return r.json()["count"]


def _own(rows: list[dict], *users: User) -> list[str]:
    """The ids of these users' rows, in the queue's order."""
    wanted = {str(user.id) for user in users}
    return [row["id"] for row in rows if row["id"] in wanted]


def _ids(*users: User) -> list[str]:
    return [str(user.id) for user in users]


# --- Count agrees with the queue, for every status and kind -------------------------


# label -> the kyc_status its rows carry
LABEL_STATUS = {
    "pending": "pending",
    "submitted": "submitted",
    "verified": "verified",
    "rejected": "rejected",
    "auto": "verified",
    "auto_too_old": "verified",
}
# ?status= -> the labels it lists
STATUS_LABELS = {
    "submitted": {"submitted"},
    "verified": {"verified", "auto", "auto_too_old"},
    "rejected": {"rejected"},
    "pending": {"pending"},
    "auto_approved": {"auto"},
    "all": {"submitted", "verified", "rejected", "auto", "auto_too_old"},
}


async def _every_state(db) -> dict[str, tuple[str, str]]:
    """One SP and one team Pro in each state. {user id: (kind, label)}."""
    bot = await _bot(db)
    now = datetime.now(UTC)
    states = {
        "pending": (KYCStatus.PENDING, {}),
        "submitted": (KYCStatus.SUBMITTED, {"kyc_submitted_at": _at(0)}),
        "verified": (KYCStatus.VERIFIED, {"kyc_submitted_at": _at(0), "kyc_reviewed_at": _at(1)}),
        "rejected": (
            KYCStatus.REJECTED,
            {
                "kyc_submitted_at": _at(0),
                "kyc_reviewed_at": _at(1),
                "kyc_rejection_reason": "Blurry",
            },
        ),
        "auto": (
            KYCStatus.VERIFIED,
            {
                "kyc_submitted_at": _at(0),
                "kyc_reviewed_at": now - timedelta(days=1),
                "kyc_reviewed_by": bot.id,
            },
        ),
        "auto_too_old": (
            KYCStatus.VERIFIED,
            {
                "kyc_submitted_at": _at(0),
                "kyc_reviewed_at": now - timedelta(days=8),
                "kyc_reviewed_by": bot.id,
            },
        ),
    }
    owner, _ = await _sp_at(db, KYCStatus.VERIFIED, business_name="Zayn Co")
    seeded: dict[str, tuple[str, str]] = {}
    for label, (kyc, fields) in states.items():
        sp, _ = await _sp_at(db, kyc, **fields)
        seeded[str(sp.id)] = ("sp", label)
        pro, _ = await _pro_at(db, owner, kyc, **fields)
        seeded[str(pro.id)] = ("pro", label)
    return seeded


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("status", ALL_STATUSES)
async def test_count_matches_the_queue_and_rows_carry_their_status(
    client, db_session, auth_headers, status, kind
):
    seeded = await _every_state(db_session)
    headers = await _admin_headers(db_session, auth_headers)

    rows = await _queue(client, headers, status=status, kind=kind)

    assert await _count(client, headers, status=status, kind=kind) == len(rows)
    assert len({(row["kind"], row["id"]) for row in rows}) == len(rows), "no row listed twice"
    listed = {seeded[row["id"]] for row in rows if row["id"] in seeded}
    kinds = {"sp", "pro"} if kind == "all" else {kind}
    assert listed == {(k, label) for k in kinds for label in STATUS_LABELS[status]}
    for row in rows:
        if row["id"] in seeded:
            assert row["kyc_status"] == LABEL_STATUS[seeded[row["id"]][1]]
            assert row["kind"] == seeded[row["id"]][0]


async def test_defaults_are_unchanged_and_the_same_on_both_routes(client, db_session, auth_headers):
    await _every_state(db_session)
    headers = await _admin_headers(db_session, auth_headers)

    default = (await client.get(QUEUE, headers=headers)).json()
    explicit = (
        await client.get(QUEUE, params={"status": "submitted", "kind": "sp"}, headers=headers)
    ).json()

    assert default == explicit
    assert {row["kind"] for row in default} == {"sp"}
    assert {row["kyc_status"] for row in default} == {"submitted"}
    assert await _count(client, headers) == await _count(
        client, headers, status="submitted", kind="sp"
    )


@pytest.mark.parametrize("url", [QUEUE, COUNT])
async def test_unknown_filters_are_422_on_both_routes(client, db_session, auth_headers, url):
    headers = await _admin_headers(db_session, auth_headers)

    r = await client.get(url, params={"status": "approved"}, headers=headers)
    assert r.status_code == 422
    assert "'auto_approved' or 'all'" in r.text

    r = await client.get(url, params={"kind": "everyone"}, headers=headers)
    assert r.status_code == 422


# --- What is left out ---------------------------------------------------------------


SELF_PRO_STATUS = {
    "submitted": KYCStatus.SUBMITTED,
    "verified": KYCStatus.VERIFIED,
    "rejected": KYCStatus.REJECTED,
    "pending": KYCStatus.PENDING,
    "auto_approved": KYCStatus.VERIFIED,
    "all": KYCStatus.SUBMITTED,
}


@pytest.mark.parametrize("status", ALL_STATUSES)
async def test_a_pro_for_self_is_never_listed_or_counted(client, db_session, auth_headers, status):
    headers = await _admin_headers(db_session, auth_headers)
    sp, _ = await _sp_at(db_session, KYCStatus.PENDING)
    before = await _count(client, headers, status=status, kind="pro")
    bot = await _bot(db_session)

    self_pro = Professional(
        service_provider_id=sp.id,
        user_id=sp.id,
        display_name="Owner",
        kyc_status=SELF_PRO_STATUS[status],
        kyc_submitted_at=_at(0),
        kyc_reviewed_at=datetime.now(UTC),
        kyc_reviewed_by=bot.id,
    )
    db_session.add(self_pro)
    await db_session.flush()

    assert await _count(client, headers, status=status, kind="pro") == before
    rows = await _queue(client, headers, status=status, kind="pro")
    assert str(self_pro.id) not in {row["professional_id"] for row in rows}


async def test_status_all_leaves_out_rows_with_no_documents(client, db_session, auth_headers):
    headers = await _admin_headers(db_session, auth_headers)
    before = await _count(client, headers, status="all", kind="all")
    sp, _ = await _sp_at(db_session, KYCStatus.PENDING)
    pro, _ = await _pro_at(db_session, sp, KYCStatus.PENDING)

    assert await _count(client, headers, status="all", kind="all") == before
    assert _own(await _queue(client, headers, status="all", kind="all"), sp, pro) == []
    # They are still there for anyone asking for that state by name.
    pending = await _queue(client, headers, status="pending", kind="all")
    assert set(_own(pending, sp, pro)) == set(_ids(sp, pro))


@pytest.mark.parametrize("status", ["submitted", "all"])
async def test_a_deleted_sp_is_gone_from_queue_and_count(client, db_session, auth_headers, status):
    headers = await _admin_headers(db_session, auth_headers)
    sp, _ = await _sp_at(db_session, KYCStatus.SUBMITTED, kyc_submitted_at=_at(0))
    before = await _count(client, headers, status=status)
    assert _own(await _queue(client, headers, status=status), sp) == _ids(sp)

    await AccountService(db_session).delete_account(sp.id)
    await db_session.flush()

    assert await _count(client, headers, status=status) == before - 1
    assert _own(await _queue(client, headers, status=status), sp) == []


@pytest.mark.parametrize("status", ["submitted", "all"])
async def test_a_deleted_pro_is_gone_from_queue_and_count(client, db_session, auth_headers, status):
    headers = await _admin_headers(db_session, auth_headers)
    owner = await _sp(db_session)
    member, _ = await _pro_at(db_session, owner, KYCStatus.SUBMITTED, kyc_submitted_at=_at(0))
    before = await _count(client, headers, status=status, kind="pro")
    assert _own(await _queue(client, headers, status=status, kind="pro"), member) == _ids(member)

    await AccountService(db_session).delete_account(member.id)
    await db_session.flush()

    assert await _count(client, headers, status=status, kind="pro") == before - 1
    assert _own(await _queue(client, headers, status=status, kind="pro"), member) == []


async def test_deleted_at_alone_keeps_a_pro_out(client, db_session, auth_headers):
    """Deletion also deactivates the Pro row, which would hide this rule."""
    headers = await _admin_headers(db_session, auth_headers)
    owner, _ = await _sp_at(db_session, KYCStatus.VERIFIED)
    member, _ = await _pro_at(db_session, owner, KYCStatus.SUBMITTED, kyc_submitted_at=_at(0))
    listed = await _count(client, headers, kind="pro")

    member.deleted_at = datetime.now(UTC)
    await db_session.flush()

    assert await _count(client, headers, kind="pro") == listed - 1
    assert _own(await _queue(client, headers, kind="pro"), member) == []


async def test_a_removed_pro_is_gone_then_returns_when_re_added(
    client, db_session, auth_headers, monkeypatch
):
    monkeypatch.setattr(sms_client.settings, "MSG91_INVITE_TEMPLATE_ID", "")
    headers = await _admin_headers(db_session, auth_headers)
    owner = await _sp(db_session)
    member = await UserFactory.create(db_session, role="consumer")
    invite = (await _invite(client, auth_headers, owner, member.phone)).json()
    joined = (await _accept(client, auth_headers, member, invite["token"])).json()
    pro = await db_session.get(Professional, uuid.UUID(joined["id"]))
    pro.kyc_status = KYCStatus.SUBMITTED
    pro.kyc_submitted_at = _at(0)
    await db_session.flush()
    listed = await _count(client, headers, kind="pro")
    assert _own(await _queue(client, headers, kind="pro"), member) == _ids(member)

    r = await client.delete(f"{TEAM}/{pro.id}", headers=auth_headers(str(owner.id), "provider"))
    assert r.status_code < 300, r.text

    assert await _count(client, headers, kind="pro") == listed - 1
    assert _own(await _queue(client, headers, kind="pro"), member) == []

    invite = (await _invite(client, auth_headers, owner, member.phone)).json()
    rejoined = (await _accept(client, auth_headers, member, invite["token"])).json()
    assert rejoined["id"] == str(pro.id), "the same row"

    assert await _count(client, headers, kind="pro") == listed
    assert _own(await _queue(client, headers, kind="pro"), member) == _ids(member)


async def test_a_deactivated_user_stays_in_unless_removed_as_a_pro(
    client, db_session, auth_headers
):
    headers = await _admin_headers(db_session, auth_headers)
    sp, _ = await _sp_at(db_session, KYCStatus.SUBMITTED, kyc_submitted_at=_at(0))
    owner = await _sp(db_session)
    kept, _ = await _pro_at(db_session, owner, KYCStatus.SUBMITTED, kyc_submitted_at=_at(0))
    removed, removed_row = await _pro_at(
        db_session, owner, KYCStatus.SUBMITTED, kyc_submitted_at=_at(0)
    )
    for user in (sp, kept, removed):
        user.is_active = False
    await TeamService(db_session).remove_pro(owner.id, removed_row.id)
    await db_session.flush()

    rows = await _queue(client, headers, kind="all")

    assert set(_own(rows, sp, kept, removed)) == set(_ids(sp, kept))


# --- Sort orders ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_status_all_is_newest_submitted_first(client, db_session, auth_headers, kind):
    headers = await _admin_headers(db_session, auth_headers)
    owner, _ = await _sp_at(db_session, KYCStatus.PENDING)
    sp_new, _ = await _sp_at(db_session, KYCStatus.REJECTED, kyc_submitted_at=_at(5))
    sp_old, _ = await _sp_at(db_session, KYCStatus.SUBMITTED, kyc_submitted_at=_at(1))
    sp_none, _ = await _sp_at(db_session, KYCStatus.VERIFIED)
    pro_mid, _ = await _pro_at(db_session, owner, KYCStatus.VERIFIED, kyc_submitted_at=_at(3))
    pro_oldest, _ = await _pro_at(db_session, owner, KYCStatus.SUBMITTED, kyc_submitted_at=_at(0))
    pro_none, _ = await _pro_at(db_session, owner, KYCStatus.REJECTED)

    rows = await _queue(client, headers, status="all", kind=kind)

    expected = {
        "sp": [sp_new, sp_old, sp_none],
        "pro": [pro_mid, pro_oldest, pro_none],
        "all": [sp_new, pro_mid, sp_old, pro_oldest, sp_none, pro_none],
    }[kind]
    assert _own(rows, *expected) == _ids(*expected)


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize(
    ("status", "kyc"), [("verified", KYCStatus.VERIFIED), ("rejected", KYCStatus.REJECTED)]
)
async def test_decisions_are_newest_review_first(
    client, db_session, auth_headers, status, kyc, kind
):
    headers = await _admin_headers(db_session, auth_headers)
    owner, _ = await _sp_at(db_session, KYCStatus.PENDING)
    # Submit times differ from the expected order in both directions, so sorting
    # on them, oldest or newest first, would fail.
    sp_new, _ = await _sp_at(db_session, kyc, kyc_submitted_at=_at(4), kyc_reviewed_at=_at(9))
    sp_old, _ = await _sp_at(db_session, kyc, kyc_submitted_at=_at(0), kyc_reviewed_at=_at(5))
    sp_none, _ = await _sp_at(db_session, kyc, kyc_submitted_at=_at(8))
    pro_mid, _ = await _pro_at(
        db_session, owner, kyc, kyc_submitted_at=_at(5), kyc_reviewed_at=_at(7)
    )
    pro_oldest, _ = await _pro_at(
        db_session, owner, kyc, kyc_submitted_at=_at(1), kyc_reviewed_at=_at(3)
    )
    pro_none, _ = await _pro_at(db_session, owner, kyc, kyc_submitted_at=_at(9))

    rows = await _queue(client, headers, status=status, kind=kind)

    expected = {
        "sp": [sp_new, sp_old, sp_none],
        "pro": [pro_mid, pro_oldest, pro_none],
        "all": [sp_new, pro_mid, sp_old, pro_oldest, sp_none, pro_none],
    }[kind]
    assert _own(rows, *expected) == _ids(*expected)


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_submitted_is_still_oldest_first(client, db_session, auth_headers, kind):
    headers = await _admin_headers(db_session, auth_headers)
    owner, _ = await _sp_at(db_session, KYCStatus.PENDING)
    sp_new, _ = await _sp_at(db_session, KYCStatus.SUBMITTED, kyc_submitted_at=_at(5))
    sp_old, _ = await _sp_at(db_session, KYCStatus.SUBMITTED, kyc_submitted_at=_at(1))
    pro_mid, _ = await _pro_at(db_session, owner, KYCStatus.SUBMITTED, kyc_submitted_at=_at(3))
    sp_none, _ = await _sp_at(db_session, KYCStatus.SUBMITTED)

    rows = await _queue(client, headers, status="submitted", kind=kind)

    expected = {
        "sp": [sp_old, sp_new, sp_none],
        "pro": [pro_mid],
        "all": [sp_old, pro_mid, sp_new, sp_none],
    }[kind]
    assert _own(rows, *expected) == _ids(*expected)


async def test_auto_approved_is_still_newest_review_first(client, db_session, auth_headers):
    headers = await _admin_headers(db_session, auth_headers)
    bot = await _bot(db_session)
    now = datetime.now(UTC)
    owner, _ = await _sp_at(db_session, KYCStatus.PENDING)
    older, _ = await _sp_at(
        db_session,
        KYCStatus.VERIFIED,
        kyc_reviewed_by=bot.id,
        kyc_reviewed_at=now - timedelta(days=2),
    )
    newer, _ = await _pro_at(
        db_session,
        owner,
        KYCStatus.VERIFIED,
        kyc_reviewed_by=bot.id,
        kyc_reviewed_at=now - timedelta(days=1),
    )

    rows = await _queue(client, headers, status="auto_approved", kind="all")

    assert _own(rows, newer, older) == _ids(newer, older)


# --- Paging ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_status_all_pages_without_repeats_or_gaps(client, db_session, auth_headers, kind):
    headers = await _admin_headers(db_session, auth_headers)
    owner, _ = await _sp_at(db_session, KYCStatus.PENDING)
    seeded: list[User] = []
    # Equal times on purpose, within and across the kinds: the tie-break's job.
    for hour in (0, 0, 0, 1, 2, 2, 3):
        sp, _ = await _sp_at(db_session, KYCStatus.SUBMITTED, kyc_submitted_at=_at(hour))
        pro, _ = await _pro_at(db_session, owner, KYCStatus.REJECTED, kyc_submitted_at=_at(hour))
        seeded += [sp, pro]

    params = {"status": "all", "kind": kind}
    whole = (await client.get(QUEUE, params={**params, "per_page": 200}, headers=headers)).json()
    walked: list[dict] = []
    page = 1
    while True:
        r = await client.get(QUEUE, params={**params, "page": page, "per_page": 3}, headers=headers)
        assert r.status_code == 200, r.text
        assert len(r.json()) <= 3
        if not r.json():
            break
        walked += r.json()
        page += 1

    keys = [(row["kind"], row["id"]) for row in walked]
    assert len(set(keys)) == len(keys), "a row repeated across pages"
    assert walked[: len(whole)] == whole
    assert len(walked) == await _count(client, headers, **params)
    wanted = {"sp": 7, "pro": 7, "all": 14}[kind]
    assert len(_own(walked, *seeded)) == wanted


@pytest.mark.parametrize("kind", ["sp", "pro"])
async def test_equal_times_fall_back_to_the_row_id(client, db_session, auth_headers, kind):
    """Postgres tends to return a small tied set in insertion order, so checking
    only that pages don't repeat wouldn't notice the tie-break going missing.
    Eight random ids in insertion order would be a one in 40,320 coincidence."""
    headers = await _admin_headers(db_session, auth_headers)
    owner, _ = await _sp_at(db_session, KYCStatus.PENDING)
    seeded = []
    for _ in range(8):
        if kind == "sp":
            seeded.append(await _sp_at(db_session, KYCStatus.REJECTED, kyc_submitted_at=_at(0)))
        else:
            seeded.append(
                await _pro_at(db_session, owner, KYCStatus.REJECTED, kyc_submitted_at=_at(0))
            )

    rows = await _queue(client, headers, status="all", kind=kind)

    by_row_id = [user for user, row in sorted(seeded, key=lambda pair: pair[1].id)]
    assert _own(rows, *by_row_id) == _ids(*by_row_id)


# --- Who can read the count -----------------------------------------------------------


@pytest.mark.parametrize("role", list(AdminRole))
async def test_every_admin_role_reads_the_count(client, db_session, role):
    admin = await provision_admin(
        db_session, f"q{uuid.uuid4().hex[:10]}@kalpai.in", role, "correct-horse-battery"
    )
    await db_session.commit()
    token = create_admin_session_token(admin.id, uuid.uuid4().hex)

    r = await client.get(COUNT, headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    assert isinstance(r.json()["count"], int)


async def test_a_consumer_cannot_read_the_count(client, db_session, auth_headers):
    consumer = await UserFactory.create(db_session, role="consumer")
    r = await client.get(COUNT, headers=auth_headers(str(consumer.id)))
    assert r.status_code == 403
