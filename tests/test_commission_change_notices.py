"""SVC-486: the notices to SPs, the email and SMS senders, and the email check
on ``PATCH /users/me``.

- the task, on its own connection (so its rows are committed and cleaned up):
  who gets a notice (an active price on the variant and an active account, and
  nobody else), one row per SP per channel with each status (sent, failed,
  skipped_no_email, skipped_no_token, mocked), a re-run sending nothing twice,
  a cancellation (its own copy, no SMS), and the "from today" copy;
- ``email_client``: the mock path, and a real send through a stub client (a
  boto3 client is never built in tests);
- ``sms_client.send_commission_change``: the mock path, and the real body with
  each var cut to 30 characters;
- ``send_multicast``: the old return shape is unchanged without ``per_token``;
- ``PATCH /users/me``: trimmed, lower-cased, a bad shape 422, "" clears it.
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.category import Category
from app.models.commission_change import CommissionChange, CommissionChangeNotice
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.models.user import User
from app.tasks import commission_tasks
from app.tasks import notification_tasks as nt
from app.utils import email_client, fcm_client, sms_client
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory

IST = timezone(timedelta(hours=5, minutes=30))
EFFECTIVE = date(2026, 12, 6)


@pytest.fixture(autouse=True)
def i18n_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "I18N_MODE", "off")
    monkeypatch.setattr(get_settings(), "I18N_TESTER_PHONES", "")


@pytest.fixture(autouse=True)
def mocked_channels(monkeypatch):
    """Email and SMS in mock mode unless a test says otherwise."""
    monkeypatch.setattr(get_settings(), "EMAIL_FROM", "")
    monkeypatch.setattr(sms_client.settings, "MSG91_COMMISSION_TEMPLATE_ID", "")


# ---------------------------------------------------------------------------
# a committed world the task can see
# ---------------------------------------------------------------------------


class _World:
    def __init__(self, db):
        self.db = db
        self.user_ids: list[uuid.UUID] = []
        self.service_ids: list[uuid.UUID] = []
        self.category_ids: list[uuid.UUID] = []

    async def catalogue(self):
        tag = uuid.uuid4().hex[:8]
        category = Category(name=f"Cat486 {tag}", slug=f"cat486-{tag}")
        self.db.add(category)
        await self.db.flush()
        self.category_ids.append(category.id)
        service = Service(
            category_id=category.id,
            name="Bathroom deep cleaning",
            base_price=Decimal("1000.00"),
            duration_minutes=60,
        )
        self.db.add(service)
        await self.db.flush()
        self.service_ids.append(service.id)
        variant = ServiceVariant(
            service_id=service.id,
            label="3 bathrooms",
            base_price=Decimal("1000.00"),
            platform_commission=Decimal("200.00"),
            provider_payout=Decimal("800.00"),
            gst_percent=Decimal("0"),
            sort_order=0,
        )
        other = ServiceVariant(
            service_id=service.id,
            label="1 bathroom",
            base_price=Decimal("500.00"),
            platform_commission=Decimal("100.00"),
            provider_payout=Decimal("400.00"),
            gst_percent=Decimal("0"),
            sort_order=1,
        )
        self.db.add_all([variant, other])
        await self.db.flush()
        return service, variant, other

    async def user(self, role="provider", *, email=None, language=None, **kw):
        user = await UserFactory.create(self.db, role=role, **kw)
        if role != "admin":  # an admin's email is its console login
            user.email = email
        user.preferred_language = language
        self.user_ids.append(user.id)
        await self.db.flush()
        return user

    async def sp(self, service, variant, *, price_active=True, **kw):
        user = await self.user(**kw)
        self.db.add(ProviderProfile(user_id=user.id, kyc_status=KYCStatus.VERIFIED))
        await self.db.flush()
        if variant is not None:
            self.db.add(
                SpServicePricing(
                    sp_id=user.id,
                    service_id=service.id,
                    service_variant_id=variant.id,
                    price_amount=Decimal("1100.00"),
                    price_unit="PER_VISIT",
                    is_active=price_active,
                )
            )
            await self.db.flush()
        return user

    async def change(self, variant, *, created=None, status="scheduled", **kw):
        admin = await self.user("admin")
        row = CommissionChange(
            service_variant_id=variant.id,
            old_pct=Decimal("20"),
            new_pct=kw.pop("new_pct", Decimal("25")),
            effective_from=kw.pop("effective_from", EFFECTIVE),
            status=status,
            created_by=admin.id,
            created_at=created or datetime(2026, 11, 22, 10, tzinfo=IST),
            **kw,
        )
        if status == "cancelled":
            row.cancelled_by, row.cancelled_at = admin.id, datetime.now(UTC)
        self.db.add(row)
        await self.db.flush()
        return row


@pytest.fixture
async def world(db_session):
    w = _World(db_session)
    yield w
    await db_session.rollback()
    variants = select(ServiceVariant.id).where(ServiceVariant.service_id.in_(w.service_ids))
    changes = select(CommissionChange.id).where(CommissionChange.service_variant_id.in_(variants))
    for stmt in (
        delete(CommissionChangeNotice).where(CommissionChangeNotice.change_id.in_(changes)),
        delete(CommissionChange).where(CommissionChange.service_variant_id.in_(variants)),
        delete(SpServicePricing).where(SpServicePricing.sp_id.in_(w.user_ids)),
        delete(Professional).where(Professional.user_id.in_(w.user_ids)),
        delete(ProviderProfile).where(ProviderProfile.user_id.in_(w.user_ids)),
        delete(ServiceVariant).where(ServiceVariant.service_id.in_(w.service_ids)),
        delete(Service).where(Service.id.in_(w.service_ids)),
        delete(Category).where(Category.id.in_(w.category_ids)),
        delete(User).where(User.id.in_(w.user_ids)),
    ):
        await db_session.execute(stmt)
    await db_session.commit()


def _in_thread(fn):
    """The task bridges to async for SMS with ``asyncio.run``, as a Celery
    worker can; pytest's own loop is running here, so run it on a thread."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


def _run(change, kind="scheduled", *, push_results=None, push_error=None):
    """Run the real task eagerly. ``push_results`` maps a token to ``(ok,
    error)``; any other token succeeds. Returns (multicasts, task result)."""
    engine = create_engine(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    )
    sent: list[dict] = []

    def _send(**kwargs):
        sent.append(kwargs)
        if push_error:
            raise push_error
        results = [(push_results or {}).get(t, (True, None)) for t in kwargs["tokens"]]
        return {"success_count": 0, "failure_count": 0, "results": results, "mocked": False}

    try:
        with (
            patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)),
            patch.object(nt, "send_multicast", side_effect=_send),
        ):
            result = _in_thread(
                lambda: commission_tasks.send_commission_change_notices.apply(
                    kwargs={"change_id": str(change.id), "kind": kind}
                ).get()
            )
    finally:
        engine.dispose()
    return sent, result


async def _notices(db, change) -> dict[tuple, tuple]:
    rows = (
        await db.scalars(
            select(CommissionChangeNotice)
            .where(CommissionChangeNotice.change_id == change.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return {(r.sp_id, r.kind, r.channel): (r.status, r.error) for r in rows}


# ---------------------------------------------------------------------------
# the task
# ---------------------------------------------------------------------------


async def test_every_sp_pricing_the_variant_gets_one_row_per_channel(db_session, world):
    service, variant, other = await world.catalogue()
    full = await world.sp(service, variant, email="full@example.com")
    bare = await world.sp(service, variant, fcm_token=None)
    excluded = [
        await world.sp(service, variant, price_active=False),
        await world.sp(service, variant, is_active=False),
        await world.sp(service, other),  # prices another option only
        await world.sp(service, None),  # no price at all
    ]
    # A team Pro of ``full``: prices are per SP, so never a recipient.
    pro = await world.user("consumer", fcm_token="fcm-team-pro")
    db_session.add(Professional(user_id=pro.id, service_provider_id=full.id, display_name="Pro"))
    change = await world.change(variant)
    await db_session.commit()

    sent, summary = _run(change)

    assert len(sent) == 1
    assert sent[0]["tokens"] == [full.fcm_token]
    assert sent[0]["per_token"] is True
    assert sent[0]["title"] == "Commission change notice"
    assert sent[0]["body"] == (
        'Bathroom deep cleaning - "3 bathrooms": the platform commission changes from 20% '
        "to 25% from 06 Dec 2026. This is your notice."
    )
    assert sent[0]["data"] == {
        "type": "pricing",
        "event": "commission_change",
        "service_id": str(service.id),
        "variant_id": str(variant.id),
        "change_id": str(change.id),
    }
    assert await _notices(db_session, change) == {
        (full.id, "scheduled", "push"): ("sent", None),
        (full.id, "scheduled", "email"): ("mocked", None),
        (full.id, "scheduled", "sms"): ("mocked", None),
        (bare.id, "scheduled", "push"): ("skipped_no_token", None),
        (bare.id, "scheduled", "email"): ("skipped_no_email", None),
        (bare.id, "scheduled", "sms"): ("mocked", None),
    }
    assert not {u.id for u in excluded} & {k[0] for k in await _notices(db_session, change)}
    assert summary == {
        "push": {"sent": 1, "skipped_no_token": 1},
        "email": {"mocked": 1, "skipped_no_email": 1},
        "sms": {"mocked": 2},
    }

    # A second run finds every row and sends nothing.
    again, summary = _run(change)
    assert again == []
    assert summary == {"push": {}, "email": {}, "sms": {}}


async def test_failures_are_recorded_with_their_error(db_session, world, monkeypatch):
    service, variant, _ = await world.catalogue()
    ok = await world.sp(service, variant, email="ok@example.com")
    bad = await world.sp(service, variant, email="bad@example.com")
    change = await world.change(variant)
    await db_session.commit()

    def _email(*, to, **_):
        if to == "bad@example.com":
            raise RuntimeError("MessageRejected: address not verified")
        return "sent"

    monkeypatch.setattr(email_client, "send_email", _email)
    sms = AsyncMock(side_effect=[True, sms_client.SMSDeliveryError("DLT mismatch")])
    with patch("app.utils.sms_client.send_commission_change", sms):
        _run(change, push_results={bad.fcm_token: (False, "Requested entity was not found.")})

    rows = await _notices(db_session, change)
    assert rows[(ok.id, "scheduled", "push")] == ("sent", None)
    assert rows[(bad.id, "scheduled", "push")] == ("failed", "Requested entity was not found.")
    assert rows[(ok.id, "scheduled", "email")] == ("sent", None)
    assert rows[(bad.id, "scheduled", "email")] == (
        "failed",
        "MessageRejected: address not verified",
    )
    statuses = sorted(rows[(u.id, "scheduled", "sms")][0] for u in (ok, bad))
    assert statuses == ["failed", "sent"]
    [sms_call, _] = sms.await_args_list
    assert sms_call.args[1:] == ("Bathroom deep cleaning", "3 bathrooms", "20", "25", "06 Dec 2026")


async def test_a_push_send_that_raises_fails_that_language_only(db_session, world):
    service, variant, _ = await world.catalogue()
    sp = await world.sp(service, variant)
    change = await world.change(variant)
    await db_session.commit()
    _run(change, push_error=RuntimeError("FCM down"))
    rows = await _notices(db_session, change)
    assert rows[(sp.id, "scheduled", "push")] == ("failed", "FCM down")
    assert rows[(sp.id, "scheduled", "email")] == ("skipped_no_email", None)


async def test_a_cancellation_has_its_own_copy_and_no_sms(db_session, world):
    service, variant, _ = await world.catalogue()
    sp = await world.sp(service, variant, email="sp@example.com", language="hi")
    change = await world.change(variant, status="cancelled")
    await db_session.commit()

    sent, _ = _run(change, "cancelled")
    assert sent[0]["title"] == "Commission change cancelled"
    assert "Your commission stays at 20%." in sent[0]["body"]
    assert await _notices(db_session, change) == {
        (sp.id, "cancelled", "push"): ("sent", None),
        (sp.id, "cancelled", "email"): ("mocked", None),
    }


async def test_a_decrease_that_applied_at_once_says_from_today(db_session, world):
    service, variant, _ = await world.catalogue()
    await world.sp(service, variant)
    change = await world.change(
        variant,
        new_pct=Decimal("15"),
        effective_from=date(2026, 11, 22),
        created=datetime(2026, 11, 22, 10, tzinfo=IST),
        status="applied",
        applied_at=datetime.now(UTC),
        old_platform_commission=Decimal("200"),
        old_provider_payout=Decimal("800"),
        new_platform_commission=Decimal("150"),
        new_provider_payout=Decimal("850"),
    )
    await db_session.commit()
    sent, _ = _run(change)
    assert (sent[0]["title"], sent[0]["body"]) == (
        "Commission lowered",
        'Bathroom deep cleaning - "3 bathrooms": the platform commission is now 15% '
        "(was 20%), from today.",
    )


async def test_a_change_that_is_gone_sends_nothing():
    engine = create_engine(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    )
    try:
        with patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)):
            result = commission_tasks.send_commission_change_notices.apply(
                kwargs={"change_id": str(uuid.uuid4()), "kind": "scheduled"}
            )
    finally:
        engine.dispose()
    assert result.get() == {}


def test_every_language_has_the_commission_copy():
    for key in ("commission_change", "commission_change_today", "commission_change_cancelled"):
        for lang in ("en", "hi", "mr"):
            title, body = nt.provider_push_copy(
                key, lang, service="S", label="L", old_pct="20", new_pct="25", date="06 Dec 2026"
            )
            assert title and "25%" in body and "{" not in body


# ---------------------------------------------------------------------------
# email
# ---------------------------------------------------------------------------


def test_email_without_a_sender_is_only_logged(caplog):
    with caplog.at_level(logging.INFO, logger="app.utils.email_client"):
        outcome = email_client.send_email(to="a@b.in", subject="Hi", text="t", html="<p>t</p>")
    assert outcome == "mocked"
    assert "[MOCK EMAIL] to=a@b.in subject=Hi" in caplog.text


def test_email_with_a_sender_goes_through_ses(monkeypatch):
    calls: list[dict] = []
    stub = SimpleNamespace(send_email=lambda **kw: calls.append(kw))
    regions: list[str] = []

    def _client(region):
        regions.append(region)
        return stub

    monkeypatch.setattr(get_settings(), "EMAIL_FROM", "no-reply@eazyserv.com")
    monkeypatch.setattr(email_client, "_ses_client", _client)
    assert email_client.send_email(to="a@b.in", subject="S", text="T", html="H") == "sent"
    assert regions == ["ap-south-1"]
    assert calls == [
        {
            "Source": "no-reply@eazyserv.com",
            "Destination": {"ToAddresses": ["a@b.in"]},
            "Message": {
                "Subject": {"Data": "S", "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": "T", "Charset": "UTF-8"},
                    "Html": {"Data": "H", "Charset": "UTF-8"},
                },
            },
        }
    ]


def test_the_notice_email_states_the_change():
    notice = commission_tasks._Notice(
        service_id="s",
        variant_id="v",
        service="Bathroom <deep> cleaning",
        label="3 bathrooms",
        old_pct="20",
        new_pct="25",
        date="06 Dec 2026",
        push_key="commission_change",
    )
    subject, text, page = commission_tasks._email_copy(notice, "scheduled")
    assert subject == "Commission change: Bathroom <deep> cleaning - 3 bathrooms"
    for line in (
        "Current commission: 20%",
        "New commission: 25%",
        "Takes effect: 06 Dec 2026",
        "This email is your notice of the change.",
    ):
        assert line in text
    assert "Bathroom &lt;deep&gt; cleaning" in page and "<deep>" not in page


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------


async def test_sms_without_a_template_is_only_logged(caplog):
    with (
        caplog.at_level(logging.INFO, logger="app.utils.sms_client"),
        patch("app.utils.sms_client.httpx.AsyncClient") as http,
    ):
        sent = await sms_client.send_commission_change(
            "+919876543210", "Bathroom", "3 bathrooms", "20", "25", "06 Dec 2026"
        )
    assert sent is False
    http.assert_not_called()
    assert "[MOCK COMMISSION SMS]" in caplog.text


async def test_sms_with_a_template_sends_five_vars_cut_to_30(monkeypatch):
    monkeypatch.setattr(sms_client.settings, "MSG91_COMMISSION_TEMPLATE_ID", "tpl_commission")
    with patch("app.utils.sms_client.httpx.AsyncClient") as http:
        post = AsyncMock(return_value=httpx.Response(200, json={"type": "success"}))
        http.return_value.__aenter__.return_value.post = post
        sent = await sms_client.send_commission_change(
            "+919876543210", "S" * 40, "O" * 31, "20", "25", "06 Dec 2026"
        )
    assert sent is True
    body = post.await_args.kwargs["json"]
    assert body["template_id"] == "tpl_commission"
    assert body["recipients"] == [
        {
            "mobiles": "919876543210",
            "var1": "S" * 30,
            "var2": "O" * 30,
            "var3": "20",
            "var4": "25",
            "var5": "06 Dec 2026",
        }
    ]


# ---------------------------------------------------------------------------
# send_multicast's shape
# ---------------------------------------------------------------------------


def test_send_multicast_keeps_its_old_shape_without_per_token(monkeypatch):
    monkeypatch.setattr(fcm_client, "is_configured", lambda: False)
    assert fcm_client.send_multicast(["a", "b"], "t", "b") == {
        "success_count": 2,
        "failure_count": 0,
    }
    assert fcm_client.send_multicast([], "t", "b") == {"success_count": 0, "failure_count": 0}
    assert fcm_client.send_multicast(["a"], "t", "b", per_token=True) == {
        "success_count": 1,
        "failure_count": 0,
        "results": [(True, None)],
        "mocked": True,
    }


def test_send_multicast_per_token_results_in_token_order(monkeypatch):
    responses = [
        SimpleNamespace(success=True, exception=None),
        SimpleNamespace(success=False, exception=RuntimeError("Unregistered")),
    ]
    batch = SimpleNamespace(success_count=1, failure_count=1, responses=responses)
    monkeypatch.setattr(fcm_client, "is_configured", lambda: True)
    monkeypatch.setattr(fcm_client, "_get_app", lambda: None)
    monkeypatch.setattr(fcm_client.messaging, "send_each_for_multicast", lambda message: batch)
    old = fcm_client.send_multicast(["a", "b"], "t", "b")
    assert old == {"success_count": 1, "failure_count": 1}
    new = fcm_client.send_multicast(["a", "b"], "t", "b", per_token=True)
    assert new["results"] == [(True, None), (False, "Unregistered")]
    assert new["mocked"] is False


# ---------------------------------------------------------------------------
# PATCH /users/me email
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "saved"),
    [(" Pro.Name@Example.COM ", "pro.name@example.com"), ("a+b@x.co.in", "a+b@x.co.in")],
)
async def test_an_email_is_trimmed_and_lower_cased(client, db_session, auth_headers, sent, saved):
    user = await UserFactory.create(db_session, role="provider")
    r = await client.patch(
        "/api/v1/users/me", json={"email": sent}, headers=auth_headers(str(user.id), "provider")
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == saved


@pytest.mark.parametrize("sent", ["nope", "a@b", "a b@c.in", "@x.in", "a@@x.in", "a@x."])
async def test_a_bad_email_is_422(client, db_session, auth_headers, sent):
    user = await UserFactory.create(db_session)
    r = await client.patch(
        "/api/v1/users/me", json={"email": sent}, headers=auth_headers(str(user.id))
    )
    assert r.status_code == 422, r.text


async def test_an_empty_email_clears_it_and_leaving_it_out_keeps_it(
    client, db_session, auth_headers
):
    user = await UserFactory.create(db_session)
    headers = auth_headers(str(user.id))
    await client.patch("/api/v1/users/me", json={"email": "x@y.in"}, headers=headers)
    r = await client.patch("/api/v1/users/me", json={"name": "Asha"}, headers=headers)
    assert r.json()["email"] == "x@y.in"
    r = await client.patch("/api/v1/users/me", json={"email": "  "}, headers=headers)
    assert (r.status_code, r.json()["email"]) == (200, None)
