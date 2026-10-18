"""
SVC-374: ``users.preferred_language`` and provider/Pro pushes in the user's language.

Covers, in order:

- The column: NULL until chosen (never defaulted to ``en``), the DB's CHECK.
- ``GET /users/me`` exposes it; ``PATCH /users/me/language`` sets it (204) and
  refuses anything but en/hi/mr (422).
- The copy: each language has every provider/Pro string with the English
  placeholders, so a missing one fails here instead of quietly sending English.
- The task: provider/Pro events in the recipient's language, customer events
  in English whatever the recipient picked (a Pro's own booking included),
  NULL users in English.
- Account deletion nulls it.
- Provider/Pro errors carry a stable ``code``.

The migration's up/down is checked by CI (``alembic upgrade head`` + ``alembic
check``) and by hand on this ticket; here the CHECK is exercised on the
create_all schema, which carries it via the model's ``__table_args__``.
"""

import string
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.booking import BookingStatus
from app.models.user import SUPPORTED_LANGUAGES, User
from app.services.account_service import AccountService
from app.tasks import notification_tasks as nt
from app.tasks.notification_tasks import (
    NOTIFICATION_TEMPLATES,
    PROVIDER_PUSH_TRANSLATIONS,
    provider_push_copy,
    push_language,
    send_booking_notification,
    send_payout_notification,
)
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_booking_push_recipients import _address
from tests.test_push_copy_pro_brand import _job

ME = "/api/v1/users/me"
LANGUAGE = "/api/v1/users/me/language"


async def _stored_language(db, user_id) -> str | None:
    """Re-read the column straight from the DB, bypassing the identity map."""
    result = await db.execute(select(User.preferred_language).where(User.id == user_id))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# The column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_user_has_no_language(db_session):
    """NULL means "never chosen", and the app's one-time prompt keys off it."""
    user = await UserFactory.create(db_session)
    await db_session.flush()

    assert await _stored_language(db_session, user.id) is None


@pytest.mark.asyncio
async def test_the_db_refuses_an_unsupported_language(db_session):
    user = await UserFactory.create(db_session)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="ck_users_preferred_language"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(User).where(User.id == user.id).values(preferred_language="fr")
            )


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_returns_null_when_never_chosen(client, auth_headers, db_session):
    user = await UserFactory.create(db_session)

    response = await client.get(ME, headers=auth_headers(str(user.id)))

    assert response.status_code == 200, response.text
    body = response.json()
    assert "preferred_language" in body
    assert body["preferred_language"] is None


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
@pytest.mark.asyncio
async def test_patch_language_stores_it_and_get_me_returns_it(
    client, auth_headers, db_session, language
):
    user = await UserFactory.create(db_session)

    response = await client.patch(
        LANGUAGE, json={"language": language}, headers=auth_headers(str(user.id))
    )

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert await _stored_language(db_session, user.id) == language
    # The harness shares one session across requests; the flush expired
    # ``updated_at`` (onupdate), which the next response would lazy-load.
    await db_session.refresh(user)
    me = await client.get(ME, headers=auth_headers(str(user.id)))
    assert me.json()["preferred_language"] == language


@pytest.mark.parametrize(
    "body",
    [
        {"language": "fr"},
        {"language": "HI"},
        {"language": "hindi"},
        {"language": ""},
        {"language": None},
        {},
    ],
)
@pytest.mark.asyncio
async def test_patch_language_rejects_anything_else(client, auth_headers, db_session, body):
    """422, and the stored value is untouched (no way back to "never chosen")."""
    user = await UserFactory.create(db_session)
    user.preferred_language = "mr"
    await db_session.flush()

    response = await client.patch(LANGUAGE, json=body, headers=auth_headers(str(user.id)))

    assert response.status_code == 422
    assert await _stored_language(db_session, user.id) == "mr"


@pytest.mark.asyncio
async def test_patch_me_returns_the_language_too(client, auth_headers, db_session):
    user = await UserFactory.create(db_session)
    user.preferred_language = "hi"
    await db_session.flush()

    response = await client.patch(ME, json={"name": "Asha"}, headers=auth_headers(str(user.id)))

    assert response.status_code == 200, response.text
    assert response.json()["preferred_language"] == "hi"


@pytest.mark.asyncio
async def test_a_consumer_role_pro_can_set_it(client, auth_headers, db_session):
    """Every Pro is a consumer-role account, so the endpoint is open to it."""
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.patch(
        LANGUAGE, json={"language": "hi"}, headers=auth_headers(str(user.id), "consumer")
    )

    assert response.status_code == 204, response.text


@pytest.mark.asyncio
async def test_patch_language_requires_authentication(client):
    response = await client.patch(LANGUAGE, json={"language": "hi"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_patch_language_only_touches_the_caller(client, auth_headers, db_session):
    caller = await UserFactory.create(db_session)
    other = await UserFactory.create(db_session)
    await db_session.flush()

    await client.patch(LANGUAGE, json={"language": "mr"}, headers=auth_headers(str(caller.id)))

    assert await _stored_language(db_session, caller.id) == "mr"
    assert await _stored_language(db_session, other.id) is None


def test_language_is_required_in_the_openapi_user_response():
    """No default, so codegen types it ``string | null`` rather than optional."""
    from app.main import app

    schema = app.openapi()["components"]["schemas"]["UserResponse"]
    assert "preferred_language" in schema["required"]


# ---------------------------------------------------------------------------
# The copy
# ---------------------------------------------------------------------------


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@pytest.mark.parametrize("lang", ["hi", "mr"])
def test_every_provider_string_is_translated_with_the_same_placeholders(lang):
    """One assertion per language: a key, field or placeholder missing from it
    fails here rather than silently falling back to English."""
    english = nt._PROVIDER_PUSH_ENGLISH
    translated = PROVIDER_PUSH_TRANSLATIONS[lang]

    assert set(translated) == set(english)
    for key, fields in english.items():
        assert set(translated[key]) == set(fields), key
        for field, text in fields.items():
            assert _placeholders(translated[key][field]) == _placeholders(text), (key, field)
            assert translated[key][field] != text, (key, field)


@pytest.mark.parametrize("lang", ["hi", "mr"])
def test_digits_stay_latin(lang):
    """₹1,250 and "2 minutes", never Devanagari digits."""
    devanagari_digits = set("०१२३४५६७८९")
    for key, fields in PROVIDER_PUSH_TRANSLATIONS[lang].items():
        for field, text in fields.items():
            assert not devanagari_digits & set(text), (key, field)


def test_hindi_copy_samples():
    assert provider_push_copy("assigned", "hi") == ("जॉब मिला", "आपको एक नया जॉब दिया गया है।")
    assert provider_push_copy("payout.completed", "hi", amount="₹1,250")[1] == (
        "₹1,250 आपके अकाउंट में जमा हो गए हैं।"
    )


def test_marathi_copy_samples():
    assert provider_push_copy("assigned", "mr") == (
        "जॉब मिळाला",
        "तुम्हाला एक नवीन जॉब देण्यात आला आहे.",
    )
    assert provider_push_copy("update_available", "mr", version="1.4.0")[1] == (
        "EazyServ v1.4.0 तयार आहे. अपडेट करण्यासाठी टॅप करा."
    )


def test_english_copy_is_unchanged():
    assert provider_push_copy("assigned", "en") == (
        NOTIFICATION_TEMPLATES["assigned"]["title"],
        NOTIFICATION_TEMPLATES["assigned"]["body"],
    )


@pytest.mark.parametrize("value", [None, "en", "fr", ""])
def test_null_en_and_unknown_mean_english(value):
    user = MagicMock()
    user.preferred_language = value
    assert push_language(user) == "en"


# ---------------------------------------------------------------------------
# The task
# ---------------------------------------------------------------------------


@pytest.fixture
async def committed(db_session):
    """Commit test rows (the task has its own connection), then remove them."""
    created: list = []
    yield created
    for obj in reversed(created):
        await db_session.delete(obj)
    await db_session.commit()


def _sent(booking_id, event, actor_id=None, professional_id=None):
    """Run the real task; return ``(token, title, body)`` for every push."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    )
    sent = []
    with (
        patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)),
        patch.object(nt, "send_push_notification") as mock_push,
    ):
        mock_push.side_effect = lambda token, title, body, **_: sent.append((token, title, body))
        send_booking_notification(
            booking_id=str(booking_id),
            event=event,
            actor_id=actor_id,
            professional_id=professional_id,
        )
    engine.dispose()
    return sent


async def _set_language(db, user, language):
    user.preferred_language = language
    await db.commit()


HI = PROVIDER_PUSH_TRANSLATIONS["hi"]
MR = PROVIDER_PUSH_TRANSLATIONS["mr"]


@pytest.mark.asyncio
async def test_a_hindi_pro_is_assigned_in_hindi(db_session, committed):
    booking, _, member = await _job(db_session, committed, brand="Sparkle", team=True)
    await _set_language(db_session, member, "hi")

    assert _sent(booking.id, "assigned") == [
        ("pro-token", HI["assigned"]["title"], HI["assigned"]["body"])
    ]


@pytest.mark.asyncio
async def test_the_same_pros_own_booking_push_stays_english(db_session, committed):
    """The ticket's case: a Pro with ``hi`` who books a service for themselves
    gets "Booking Confirmed" in English."""
    _, sp, member = await _job(db_session, committed, brand="Sparkle", team=True)
    await _set_language(db_session, member, "hi")
    addr = await _address(db_session, member.id)
    own = await BookingFactory.create(
        db_session,
        consumer_id=member.id,
        provider_id=sp.id,
        address_id=addr.id,
        status=BookingStatus.ACCEPTED,
    )
    committed.extend([addr, own])
    await db_session.commit()

    assert _sent(own.id, "accepted") == [
        (
            "pro-token",
            NOTIFICATION_TEMPLATES["accepted"]["title"],
            NOTIFICATION_TEMPLATES["accepted"]["body"],
        )
    ]


@pytest.mark.asyncio
async def test_a_user_who_never_chose_gets_english(db_session, committed):
    booking, _, member = await _job(db_session, committed, brand="Sparkle", team=True)
    assert member.preferred_language is None

    assert _sent(booking.id, "assigned") == [
        (
            "pro-token",
            NOTIFICATION_TEMPLATES["assigned"]["title"],
            NOTIFICATION_TEMPLATES["assigned"]["body"],
        )
    ]


@pytest.mark.asyncio
async def test_a_cancel_is_marathi_for_the_sp_and_pro_and_english_for_the_consumer(
    db_session, committed
):
    """An admin cancel tells all three, each by their side of the booking, not
    their pick: the consumer here picked Hindi and still gets English."""
    booking, sp, member = await _job(db_session, committed, brand="Sparkle", team=True)
    consumer = await db_session.get(User, booking.consumer_id)
    consumer.preferred_language = "hi"
    sp.preferred_language = "mr"
    member.preferred_language = "mr"
    await db_session.commit()

    import uuid

    assert _sent(booking.id, "cancelled", actor_id=str(uuid.uuid4())) == [
        (
            "consumer-token",
            NOTIFICATION_TEMPLATES["cancelled"]["title"],
            NOTIFICATION_TEMPLATES["cancelled"]["body"],
        ),
        ("sp-token", MR["cancelled"]["title"], MR["cancelled"]["body"]),
        ("pro-token", MR["job_cancelled"]["title"], MR["job_cancelled"]["body"]),
    ]


@pytest.mark.asyncio
async def test_unassigned_goes_out_in_the_pros_language(db_session, committed):
    booking, _, member = await _job(db_session, committed, brand="Sparkle", team=True)
    await _set_language(db_session, member, "hi")

    assert _sent(booking.id, "unassigned", professional_id=str(booking.professional_id)) == [
        ("pro-token", HI["unassigned"]["title"], HI["unassigned"]["body"])
    ]


@pytest.mark.asyncio
async def test_pro_declined_names_the_pro_in_the_sps_language(db_session, committed):
    booking, sp, member = await _job(db_session, committed, brand="Sparkle", team=True)
    await _set_language(db_session, sp, "hi")

    assert _sent(booking.id, "pro_declined", actor_id=str(member.id)) == [
        ("sp-token", HI["pro_declined"]["title"], "Rohan यह जॉब नहीं ले सकते। किसी और को असाइन करें।")
    ]


def _broadcast_bodies(lang: str, service_names: list[str]) -> list[tuple[str, str]]:
    """Drive the ``new_request`` broadcast with mocks; return ``(title, body)``."""
    booking = MagicMock()
    booking.status = BookingStatus.REQUESTED
    booking.consumer_address_id = "addr"
    provider = MagicMock()
    provider.fcm_token = "provider-token"
    provider.preferred_language = lang

    address = MagicMock()
    address.location = "POINT(73.8567 18.5204)"

    db = MagicMock()
    db.get.side_effect = lambda model, _id, **_: (
        booking if model.__name__ == "Booking" else address
    )
    items = []
    for name in service_names:
        item = MagicMock()
        item.service_id = f"svc-{name}"
        item.service.name = name
        items.append(item)
    db.execute.return_value.scalars.return_value.all.side_effect = [items, [provider]]
    session_cm = MagicMock()
    session_cm.__enter__.return_value = db

    calls = []
    with (
        patch("app.database.SyncSessionLocal", return_value=session_cm),
        patch.object(nt, "send_push_notification", side_effect=lambda **kw: calls.append(kw)),
    ):
        nt.send_booking_notification(booking_id="b1", event="new_request")
    return [(c["title"], c["body"]) for c in calls]


def test_a_hindi_sp_gets_the_job_request_in_hindi_with_the_service_name_untranslated():
    assert _broadcast_bodies("hi", ["Deep Cleaning"]) == [
        ("नई बुकिंग रिक्वेस्ट!", "Deep Cleaning — 2 मिनट के अंदर स्वीकार करें।")
    ]


def test_a_marathi_sp_reads_the_cart_count_in_marathi():
    assert _broadcast_bodies("mr", ["Deep Cleaning", "Sofa Cleaning", "Fan Cleaning"]) == [
        ("नवीन बुकिंग रिक्वेस्ट!", "Deep Cleaning + आणखी 2 — 2 मिनिटांत स्वीकारा.")
    ]


def test_an_sp_who_never_chose_gets_the_english_job_request():
    assert _broadcast_bodies(None, ["Deep Cleaning", "Sofa Cleaning"]) == [
        ("New Booking Request!", "Deep Cleaning + 1 more — accept within 2 minutes.")
    ]


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        (None, ("Payout Received", "₹1,250 has been credited to your account.")),
        ("hi", ("पेआउट मिल गया", "₹1,250 आपके अकाउंट में जमा हो गए हैं।")),
        ("mr", ("पेआउट मिळाले", "₹1,250 तुमच्या खात्यात जमा झाले आहेत.")),
    ],
)
def test_payout_push_is_in_the_providers_language(lang, expected):
    provider = MagicMock()
    provider.fcm_token = "provider-token"
    provider.preferred_language = lang
    db = MagicMock()
    db.get.return_value = provider
    session_cm = MagicMock()
    session_cm.__enter__.return_value = db

    calls = []
    with (
        patch("app.database.SyncSessionLocal", return_value=session_cm),
        patch.object(nt, "send_push_notification", side_effect=lambda **kw: calls.append(kw)),
    ):
        send_payout_notification(provider_id="p1", event="completed", amount_paise=125000)

    assert [(c["title"], c["body"]) for c in calls] == [expected]


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_deletion_nulls_the_language(db_session):
    user = await UserFactory.create(db_session, role="consumer")
    user.preferred_language = "mr"
    await db_session.flush()

    await AccountService(db_session).delete_account(user.id)

    assert await _stored_language(db_session, user.id) is None


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_booking_carries_a_code(client, auth_headers, db_session):
    import uuid

    sp = await UserFactory.create(db_session, role="provider")

    response = await client.post(
        f"/api/v1/bookings/{uuid.uuid4()}/release",
        headers=auth_headers(str(sp.id), "provider"),
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "BOOKING_NOT_FOUND"


@pytest.mark.asyncio
async def test_an_unsupported_upload_carries_a_code(client, auth_headers, db_session):
    """Was ``{"detail"}`` from an HTTPException; now the AppException shape."""
    sp = await UserFactory.create(db_session, role="provider")

    response = await client.post(
        "/api/v1/providers/me/photo",
        files={"photo": ("cv.pdf", b"%PDF-1.4", "application/pdf")},
        headers=auth_headers(str(sp.id), "provider"),
    )

    assert response.status_code == 415, response.text
    body = response.json()
    assert body["code"] == "UNSUPPORTED_FILE_TYPE"
    assert "unsupported file type" in body["message"]


@pytest.mark.asyncio
async def test_an_empty_upload_carries_a_code(client, auth_headers, db_session):
    sp = await UserFactory.create(db_session, role="provider")

    response = await client.post(
        "/api/v1/providers/me/photo",
        files={"photo": ("me.jpg", b"", "image/jpeg")},
        headers=auth_headers(str(sp.id), "provider"),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "EMPTY_FILE"
