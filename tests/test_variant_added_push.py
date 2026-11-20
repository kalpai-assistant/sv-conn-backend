"""SVC-487: a variant added to an active service pushes that service's SPs to price it.

- the enqueue, through the real ``call_after_commit`` hook on a real commit or
  rollback: create on an active service queues one push; a switched-off
  service, a refused create, a rolled-back create, a PATCH and a switch off or
  on queue nothing;
- the task, on its own connection (so its rows are committed and cleaned up):
  who gets it and who doesn't, a variant or service switched off since, one
  multicast per language, a failed send never sent twice, a failed read
  retried once before anything is sent.
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.category import Category
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.tasks import notification_tasks as nt
from app.tasks.notification_tasks import (
    PROVIDER_PUSH_TRANSLATIONS,
    send_variant_added_notification,
)
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_admin_catalog import _cat, _svc
from tests.test_admin_console_auth import _admin, _session
from tests.test_admin_variants import _url, _variant

TASK = nt.send_variant_added_notification
BODY = {"label": "5 BHK", "base_price": "3999", "commission_pct": "25"}


@pytest.fixture(autouse=True)
def i18n_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "I18N_MODE", "off")
    monkeypatch.setattr(get_settings(), "I18N_TESTER_PHONES", "")


@pytest.fixture
async def catalog_cleanup(db_session):
    """These tests commit for real; drop their catalogue rows afterwards. Plain
    ids, since a rollback expires the ORM objects."""
    ids: list[tuple[uuid.UUID, uuid.UUID]] = []
    yield ids
    await db_session.rollback()
    for service_id, category_id in ids:
        await db_session.execute(
            delete(ServiceVariant).where(ServiceVariant.service_id == service_id)
        )
        await db_session.execute(delete(Service).where(Service.id == service_id))
        await db_session.execute(delete(Category).where(Category.id == category_id))
    await db_session.commit()


async def _service(db, ids: list, *, is_active: bool = True) -> tuple[Service, ServiceVariant]:
    service = await _svc(db, await _cat(db), base_price=Decimal("2499.00"), is_active=is_active)
    ids.append((service.id, service.category_id))
    variant = await _variant(db, service, "2499.00", "749.70", label="3 BHK")
    return service, variant


# ---------------------------------------------------------------------------
# the enqueue
# ---------------------------------------------------------------------------


async def test_create_on_an_active_service_queues_one_push_after_commit(
    client, db_session, catalog_cleanup
):
    admin = await _admin(db_session)
    service, _ = await _service(db_session, catalog_cleanup)
    with patch.object(TASK, "delay") as delay:
        r = await client.post(_url(service), headers=_session(admin), json=BODY)
        assert r.status_code == 201, r.text
        assert delay.call_count == 0  # not committed yet
        await db_session.commit()
    new = next(v for v in r.json()["variants"] if v["label"] == "5 BHK")
    delay.assert_called_once_with(variant_id=new["id"])


async def test_create_on_a_switched_off_service_queues_nothing(client, db_session, catalog_cleanup):
    admin = await _admin(db_session)
    service, _ = await _service(db_session, catalog_cleanup, is_active=False)
    with patch.object(TASK, "delay") as delay:
        r = await client.post(_url(service), headers=_session(admin), json=BODY)
        assert r.status_code == 201, r.text
        await db_session.commit()
    delay.assert_not_called()


async def test_a_refused_create_queues_nothing(client, db_session, catalog_cleanup):
    admin = await _admin(db_session)
    service, _ = await _service(db_session, catalog_cleanup)
    with patch.object(TASK, "delay") as delay:
        r = await client.post(
            _url(service), headers=_session(admin), json={**BODY, "label": "3 bhk"}
        )
        assert (r.status_code, r.json()["code"]) == (409, "VARIANT_LABEL_TAKEN"), r.text
        await db_session.commit()
    delay.assert_not_called()


async def test_a_rolled_back_create_queues_nothing(client, db_session, catalog_cleanup):
    admin = await _admin(db_session)
    service, _ = await _service(db_session, catalog_cleanup)
    await db_session.commit()
    with patch.object(TASK, "delay") as delay:
        r = await client.post(_url(service), headers=_session(admin), json=BODY)
        assert r.status_code == 201, r.text
        await db_session.rollback()
        await db_session.commit()  # a later commit must not run the dropped push
    delay.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [{"base_price": "2799"}, {"commission_pct": "20"}, {"duration_minutes": 90}],
    ids=["price", "pct", "duration"],
)
async def test_a_patch_queues_nothing(client, db_session, catalog_cleanup, body):
    admin = await _admin(db_session)
    service, variant = await _service(db_session, catalog_cleanup)
    with patch.object(TASK, "delay") as delay:
        r = await client.patch(_url(service, variant), headers=_session(admin), json=body)
        assert r.status_code == 200, r.text
        await db_session.commit()
    delay.assert_not_called()


async def test_switching_off_and_on_queues_nothing(client, db_session, catalog_cleanup):
    admin = await _admin(db_session)
    service, variant = await _service(db_session, catalog_cleanup)
    await _variant(db_session, service, "2999.00", "899.70", label="4 BHK", sort_order=1)
    headers = _session(admin)
    with patch.object(TASK, "delay") as delay:
        off = await client.post(_url(service, variant, "/deactivate"), headers=headers)
        assert off.status_code == 200, off.text
        await db_session.commit()
        on = await client.post(_url(service, variant, "/activate"), headers=headers)
        assert on.status_code == 200, on.text
        await db_session.commit()
    delay.assert_not_called()


# ---------------------------------------------------------------------------
# the task. It runs on its own connection, so these rows are committed.
# ---------------------------------------------------------------------------


@pytest.fixture
async def committed(db_session):
    """Rows the task must see. Deleted afterwards, children first."""
    rows: list = []
    yield rows
    await db_session.rollback()
    for obj in reversed(rows):
        await db_session.delete(obj)
        await db_session.flush()
    await db_session.commit()


class _World:
    """One service with a new variant, and SPs around it."""

    def __init__(self, db, rows: list):
        self.db, self.rows = db, rows

    async def add(self, obj):
        self.db.add(obj)
        await self.db.flush()
        self.rows.append(obj)
        return obj

    async def catalogue(self, *, service_active=True, variant_active=True):
        category = await self.add(
            Category(name=f"Cat487 {uuid.uuid4().hex[:6]}", slug=f"cat487-{uuid.uuid4().hex[:8]}")
        )
        service = await self.add(
            Service(
                category_id=category.id,
                name=f"Deep Clean {uuid.uuid4().hex[:4]}",
                base_price=Decimal("2499.00"),
                duration_minutes=60,
                is_active=service_active,
            )
        )
        variant = await self.add(
            ServiceVariant(
                service_id=service.id,
                label="5 BHK",
                base_price=Decimal("3999.00"),
                platform_commission=Decimal("999.75"),
                provider_payout=Decimal("2999.25"),
                gst_percent=Decimal("0"),
                sort_order=0,
                is_active=variant_active,
            )
        )
        return service, variant

    async def user(self, role="provider", **kw):
        user = await UserFactory.create(self.db, role=role, **kw)
        self.rows.append(user)
        return user

    async def sp(self, service=None, *, kyc=KYCStatus.VERIFIED, language=None, **kw):
        user = await self.user(**kw)
        if language:
            user.preferred_language = language
        await self.add(ProviderProfile(user_id=user.id, kyc_status=kyc))
        if service is not None:
            await self.add(ProviderService(provider_id=user.id, service_id=service.id))
        return user


@pytest.fixture
def world(db_session, committed):
    return _World(db_session, committed)


def _run(variant_id, *, send_error=None, session_factory=None) -> tuple[list[dict], dict | None]:
    """Run the real task eagerly (retries included). Returns every multicast's
    kwargs and the task's result."""
    engine = create_engine(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    )
    sent: list[dict] = []

    def _send(**kwargs):
        sent.append(kwargs)
        if send_error:
            raise send_error
        return {"success_count": len(kwargs["tokens"]), "failure_count": 0}

    try:
        with (
            patch("app.database.SyncSessionLocal", session_factory or sessionmaker(bind=engine)),
            patch.object(nt, "send_multicast", side_effect=_send),
        ):
            result = send_variant_added_notification.apply(kwargs={"variant_id": str(variant_id)})
    finally:
        engine.dispose()
    return sent, result


async def test_only_the_services_verified_active_sps_with_a_token_get_it(db_session, world):
    service, variant = await world.catalogue()
    other_service, _ = await world.catalogue()
    included = await world.sp(service)
    also_included = await world.sp(service)
    excluded = [
        await world.sp(other_service),  # doesn't offer this service
        await world.sp(),  # offers nothing
        await world.sp(service, kyc=KYCStatus.PENDING),
        await world.sp(service, kyc=KYCStatus.SUBMITTED),
        await world.sp(service, kyc=KYCStatus.REJECTED),
        await world.sp(service, is_active=False),
        await world.user("consumer", fcm_token=f"fcm-c-{uuid.uuid4().hex[:8]}"),
        await world.user("admin", fcm_token=f"fcm-a-{uuid.uuid4().hex[:8]}"),
    ]
    no_token = await world.sp(service, fcm_token=None)
    # A team Pro of a qualifying SP, with a token: Pros don't set prices.
    pro = await world.user("consumer", fcm_token=f"fcm-p-{uuid.uuid4().hex[:8]}")
    await world.add(
        Professional(user_id=pro.id, service_provider_id=included.id, display_name="Team Pro")
    )
    # The SP's Pro-for-self is the same user: still one push.
    await world.add(
        Professional(user_id=included.id, service_provider_id=included.id, display_name="Self")
    )
    await db_session.commit()

    sent, result = _run(variant.id)

    assert len(sent) == 1
    assert sorted(sent[0]["tokens"]) == sorted([included.fcm_token, also_included.fcm_token])
    leaked = {u.fcm_token for u in [*excluded, pro, no_token]} & set(sent[0]["tokens"])
    assert leaked == set()
    assert sent[0]["title"] == "New option to price"
    assert sent[0]["body"] == (
        f'{service.name} now has "5 BHK". Set your price so customers can book it.'
    )
    assert sent[0]["data"] == {
        "type": "pricing",
        "event": "variant_added",
        "service_id": str(service.id),
        "variant_id": str(variant.id),
    }
    assert result.get() == {"recipient_count": 2, "success_count": 2, "failure_count": 0}


@pytest.mark.parametrize(
    ("service_active", "variant_active"), [(False, True), (True, False)], ids=["service", "variant"]
)
async def test_nothing_is_sent_once_switched_off(db_session, world, service_active, variant_active):
    service, variant = await world.catalogue(
        service_active=service_active, variant_active=variant_active
    )
    await world.sp(service)
    await db_session.commit()

    sent, _ = _run(variant.id)

    assert sent == []


def test_an_unknown_variant_sends_nothing():
    sent, _ = _run(uuid.uuid4())
    assert sent == []


async def test_nobody_to_tell_sends_nothing(db_session, world):
    _, variant = await world.catalogue()
    await db_session.commit()

    sent, result = _run(variant.id)

    assert sent == []
    assert result.get()["recipient_count"] == 0


async def test_one_multicast_per_language_each_in_its_words(db_session, world, monkeypatch):
    service, variant = await world.catalogue()
    en = await world.sp(service)
    unchosen = await world.sp(service)
    hi = await world.sp(service, language="hi")
    mr = await world.sp(service, language="mr")
    not_a_tester = await world.sp(service, language="hi")
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "I18N_MODE", "testers")
    monkeypatch.setattr(get_settings(), "I18N_TESTER_PHONES", f"{en.phone},{hi.phone},{mr.phone}")

    sent, _ = _run(variant.id)

    by_title = {push["title"]: push for push in sent}
    assert len(sent) == len(by_title) == 3
    english = by_title["New option to price"]
    assert sorted(english["tokens"]) == sorted(
        [en.fcm_token, unchosen.fcm_token, not_a_tester.fcm_token]
    )
    for lang, user in (("hi", hi), ("mr", mr)):
        copy = PROVIDER_PUSH_TRANSLATIONS[lang]["variant_added"]
        push = by_title[copy["title"]]
        assert push["tokens"] == [user.fcm_token]
        assert push["body"] == copy["body"].format(service=service.name, label="5 BHK")
        assert service.name in push["body"] and "5 BHK" in push["body"]


async def test_a_failed_send_is_never_sent_again(db_session, world):
    service, variant = await world.catalogue()
    sp = await world.sp(service)
    await db_session.commit()

    sent, result = _run(variant.id, send_error=RuntimeError("FCM down"))

    assert len(sent) == 1 and sent[0]["tokens"] == [sp.fcm_token]
    assert result.successful()
    assert result.get() == {"recipient_count": 1, "success_count": 0, "failure_count": 1}


def test_a_failed_read_is_retried_once_and_sends_nothing():
    opened: list[int] = []

    def _broken_session():
        opened.append(1)
        raise ConnectionError("db blip")

    sent, result = _run(uuid.uuid4(), session_factory=_broken_session)

    assert sent == []
    assert len(opened) == 2  # the first try and one retry
    assert result.failed()
    assert isinstance(result.result, ConnectionError)
