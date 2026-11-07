"""A rejected KYC tells the person, and a provider can read why (SVC-444)."""

from unittest.mock import patch

import pytest
from firebase_admin import messaging as fcm_messaging
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import User
from app.tasks import notification_tasks as nt
from app.tasks.notification_tasks import PROVIDER_PUSH_TRANSLATIONS, send_kyc_rejected_notification
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_admin_audit import _submitted_pro

ME = "/api/v1/providers/me"
REASON = "Aadhaar photo is blurred"
DELAY = "app.tasks.notification_tasks.send_kyc_rejected_notification.delay"


@pytest.fixture(autouse=True)
def i18n_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "I18N_MODE", "on")
    monkeypatch.setattr(get_settings(), "I18N_TESTER_PHONES", "")


async def _provider(db, status=KYCStatus.SUBMITTED) -> tuple[User, ProviderProfile]:
    user = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(user_id=user.id, kyc_status=status)
    db.add(profile)
    await db.flush()
    return user, profile


async def _reject(client, auth_headers, db, path: str, reason: str = REASON):
    admin = await UserFactory.create(db, role="admin")
    return await client.post(
        f"/api/v1/admin/{path}/kyc/reject",
        json={"reason": reason},
        headers=auth_headers(str(admin.id), "admin"),
    )


@pytest.mark.asyncio
async def test_me_has_no_reason_before_a_rejection(client, auth_headers, db_session):
    user, _ = await _provider(db_session)

    response = await client.get(ME, headers=auth_headers(str(user.id), "provider"))

    assert response.status_code == 200
    assert response.json()["kyc_rejection_reason"] is None


@pytest.mark.asyncio
async def test_me_returns_the_reason_after_a_rejection(client, auth_headers, db_session):
    user, _ = await _provider(db_session)

    with patch(DELAY):
        rejected = await _reject(client, auth_headers, db_session, f"providers/{user.id}")
    assert rejected.status_code == 200

    response = await client.get(ME, headers=auth_headers(str(user.id), "provider"))
    assert response.json()["kyc_status"] == "rejected"
    assert response.json()["kyc_rejection_reason"] == REASON


@pytest.mark.asyncio
async def test_re_submitting_clears_the_reason(client, auth_headers, db_session):
    from tests.test_kyc import _kyc_files

    user, profile = await _provider(db_session, KYCStatus.REJECTED)
    profile.kyc_rejection_reason = REASON
    await db_session.flush()

    with patch("app.services.provider_service.storage.upload_file", return_value="https://x"):
        submitted = await client.post(
            f"{ME}/kyc", files=_kyc_files(), headers=auth_headers(str(user.id), "provider")
        )
    assert submitted.status_code == 201, submitted.text

    response = await client.get(ME, headers=auth_headers(str(user.id), "provider"))
    assert response.json()["kyc_rejection_reason"] is None


@pytest.mark.asyncio
async def test_rejecting_an_sp_pushes_once_to_that_sp(client, auth_headers, db_session):
    user, _ = await _provider(db_session)

    with patch(DELAY) as delay:
        response = await _reject(client, auth_headers, db_session, f"providers/{user.id}")

    assert response.status_code == 200
    delay.assert_called_once_with(user_id=str(user.id))


@pytest.mark.asyncio
async def test_rejecting_a_pro_pushes_the_pro_and_not_their_sp(client, auth_headers, db_session):
    sp, pro = await _submitted_pro(db_session)

    with patch(DELAY) as delay:
        response = await _reject(client, auth_headers, db_session, f"pros/{pro.id}")

    assert response.status_code == 200
    delay.assert_called_once_with(user_id=str(pro.user_id))
    assert str(pro.user_id) != str(sp.id)


@pytest.mark.asyncio
async def test_a_removed_pro_is_rejected_but_not_pushed(client, auth_headers, db_session):
    """The route can still act on a removed Pro, from a stale screen or the API, but
    that person has left the team, so a push about their KYC would make no sense."""
    _, pro = await _submitted_pro(db_session)
    pro.is_active = False
    await db_session.flush()

    with patch(DELAY) as delay:
        response = await _reject(client, auth_headers, db_session, f"pros/{pro.id}")

    assert response.status_code == 200
    assert response.json()["kyc_status"] == "rejected"
    delay.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [KYCStatus.VERIFIED, KYCStatus.REJECTED, KYCStatus.PENDING])
async def test_a_refused_reject_sends_nothing(client, auth_headers, db_session, status):
    user, _ = await _provider(db_session, status)

    with patch(DELAY) as delay:
        response = await _reject(client, auth_headers, db_session, f"providers/{user.id}")

    assert response.status_code == 403
    delay.assert_not_called()


@pytest.mark.asyncio
async def test_approving_sends_no_rejection_push(client, auth_headers, db_session):
    user, _ = await _provider(db_session)
    admin = await UserFactory.create(db_session, role="admin")

    with patch(DELAY) as delay:
        response = await client.post(
            f"/api/v1/admin/providers/{user.id}/kyc/verify",
            headers=auth_headers(str(admin.id), "admin"),
        )

    assert response.status_code == 200
    delay.assert_not_called()


@pytest.mark.asyncio
async def test_a_broker_failure_doesnt_fail_a_saved_rejection(client, auth_headers, db_session):
    user, profile = await _provider(db_session)

    with patch(DELAY, side_effect=ConnectionError("broker down")):
        response = await _reject(client, auth_headers, db_session, f"providers/{user.id}")

    assert response.status_code == 200
    await db_session.refresh(profile)
    assert profile.kyc_status == KYCStatus.REJECTED
    assert profile.kyc_rejection_reason == REASON


# --- The task itself runs on its own connection, so these rows are committed.


@pytest.fixture
async def committed(db_session):
    created: list = []
    yield created
    for obj in reversed(created):
        await db_session.delete(obj)
    await db_session.commit()


def _run(user_id, side_effect=None) -> list[dict]:
    """Run the real task. Returns the keyword arguments of every push."""
    engine = create_engine(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    )
    sent: list[dict] = []

    def _send(**kwargs):
        sent.append(kwargs)
        if side_effect:
            raise side_effect

    with (
        patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)),
        patch.object(nt, "send_push_notification", side_effect=_send),
    ):
        send_kyc_rejected_notification(user_id=str(user_id))
    engine.dispose()
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "title", "body"),
    [
        (
            None,
            "Your documents were rejected",
            "Open the app to see why and submit them again.",
        ),
        (
            "hi",
            PROVIDER_PUSH_TRANSLATIONS["hi"]["kyc_rejected"]["title"],
            PROVIDER_PUSH_TRANSLATIONS["hi"]["kyc_rejected"]["body"],
        ),
        (
            "mr",
            PROVIDER_PUSH_TRANSLATIONS["mr"]["kyc_rejected"]["title"],
            PROVIDER_PUSH_TRANSLATIONS["mr"]["kyc_rejected"]["body"],
        ),
    ],
)
async def test_the_push_is_in_their_language(db_session, committed, language, title, body):
    user = await UserFactory.create(db_session, role="provider", fcm_token="live-token")
    user.preferred_language = language
    committed.append(user)
    await db_session.commit()

    sent = _run(user.id)

    assert len(sent) == 1
    assert sent[0]["token"] == "live-token"
    assert sent[0]["title"] == title
    assert sent[0]["body"] == body


@pytest.mark.asyncio
async def test_the_reason_stays_off_the_lock_screen(db_session, committed):
    user = await UserFactory.create(db_session, role="provider", fcm_token="live-token")
    profile = ProviderProfile(
        user_id=user.id, kyc_status=KYCStatus.REJECTED, kyc_rejection_reason=REASON
    )
    db_session.add(profile)
    await db_session.commit()
    committed.extend([user, profile])

    sent = _run(user.id)

    assert REASON not in repr(sent)
    assert sent[0]["data"] == {"type": "kyc", "event": "kyc_rejected"}


@pytest.mark.asyncio
async def test_no_token_sends_nothing(db_session, committed):
    user = await UserFactory.create(db_session, role="provider", fcm_token=None)
    committed.append(user)
    await db_session.commit()

    assert _run(user.id) == []


def test_an_unknown_user_sends_nothing():
    assert _run("00000000-0000-0000-0000-000000000000") == []


@pytest.mark.asyncio
async def test_a_dead_token_is_cleared(db_session, committed):
    user = await UserFactory.create(db_session, role="provider", fcm_token="dead-token")
    committed.append(user)
    await db_session.commit()

    _run(user.id, side_effect=fcm_messaging.UnregisteredError("token expired"))

    # Reads the column, not the cached object: the task wrote it on another connection.
    token = await db_session.scalar(select(User.fcm_token).where(User.id == user.id))
    assert token is None
