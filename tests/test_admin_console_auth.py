"""
SVC-422: admin console login and role gating.

Rows are really committed here (the service commits failure counts), so every
test makes its own admin with a unique email and sends its own client IP, which
keeps the login limit and Redis keys from leaking between tests.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.dependencies import verify_ws_token
from app.main import create_app
from app.models.admin_action import AdminAction
from app.models.admin_user import AdminRole, admin_placeholder_phone
from app.services.admin_auth_service import provision_admin
from app.utils.encryption import decrypt
from app.utils.security import (
    create_access_token,
    create_admin_challenge_token,
    create_admin_session_token,
    create_refresh_token,
    decode_admin_token,
    hash_password,
    verify_password,
)
from tests.factories.user_factory import UserFactory

A = "/api/v1/admin"
AUTH = f"{A}/auth"
PASSWORD = "correct-horse-battery"


def _ip() -> dict:
    """A client IP of the test's own, so tests don't share the login bucket."""
    return {"X-Forwarded-For": f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.7"}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _admin(db, role: AdminRole = AdminRole.ADMIN):
    admin = await provision_admin(db, f"a{uuid.uuid4().hex[:10]}@kalpai.in", role, PASSWORD)
    await db.commit()
    return admin


async def _login(client, email: str, password: str = PASSWORD):
    return await client.post(
        f"{AUTH}/login", json={"email": email, "password": password}, headers=_ip()
    )


async def _set_up_totp(client, admin) -> str:
    """Full first login. Returns the secret."""
    challenge = (await _login(client, admin.email)).json()["challenge_token"]
    url = (await client.post(f"{AUTH}/totp/setup", json={"challenge_token": challenge})).json()[
        "otpauth_url"
    ]
    secret = pyotp.parse_uri(url).secret
    r = await client.post(
        f"{AUTH}/totp/verify",
        json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 200, r.text
    return secret


def _session(admin, age: timedelta = timedelta(0)) -> dict:
    """A session token as if issued ``age`` ago."""
    return _bearer(
        create_admin_session_token(admin.id, uuid.uuid4().hex, timedelta(hours=12) - age)
    )


# ---------------------------------------------------------------------------
# Password step
# ---------------------------------------------------------------------------


async def test_login_returns_challenge_and_asks_for_setup_on_first_login(client, db_session):
    admin = await _admin(db_session)
    r = await _login(client, admin.email)
    assert r.status_code == 200
    body = r.json()
    assert body["requires_totp"] is True
    assert body["totp_setup_required"] is True
    assert decode_admin_token(body["challenge_token"], "admin_challenge")["sub"] == str(admin.id)


async def test_login_email_is_case_insensitive(client, db_session):
    admin = await _admin(db_session)
    assert (await _login(client, admin.email.upper())).status_code == 200


async def test_login_after_activation_does_not_ask_for_setup(client, db_session):
    admin = await _admin(db_session)
    await _set_up_totp(client, admin)
    assert (await _login(client, admin.email)).json()["totp_setup_required"] is False


async def test_unknown_email_wrong_password_locked_and_inactive_look_the_same(client, db_session):
    wrong = await _admin(db_session)
    locked = await _admin(db_session)
    locked.locked_until = datetime.now(UTC) + timedelta(hours=1)
    inactive = await _admin(db_session)
    inactive.is_active = False
    await db_session.commit()

    responses = [
        await _login(client, f"nobody-{uuid.uuid4().hex}@kalpai.in"),
        await _login(client, wrong.email, "not-the-password"),
        await _login(client, locked.email),
        await _login(client, inactive.email),
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1
    assert responses[0].json()["code"] == "INVALID_CREDENTIALS"


async def test_overlong_password_is_a_failed_login_not_a_500(client, db_session):
    admin = await _admin(db_session)
    assert (await _login(client, admin.email, "x" * 200)).status_code == 401


async def test_three_failures_lock_even_the_right_password_until_sql_unlock(client, db_session):
    admin = await _admin(db_session)
    for _ in range(3):
        assert (await _login(client, admin.email, "wrong-password")).status_code == 401

    await db_session.refresh(admin)
    assert admin.failed_login_count == 3
    assert admin.locked_until > datetime.now(UTC) + timedelta(hours=23)
    assert (await _login(client, admin.email)).status_code == 401

    # The runbook's unlock.
    admin.failed_login_count = 0
    admin.locked_until = None
    await db_session.commit()
    assert (await _login(client, admin.email)).status_code == 200


async def test_lock_runs_out_and_gives_three_fresh_attempts(client, db_session):
    admin = await _admin(db_session)
    admin.failed_login_count = 3
    admin.locked_until = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    assert (await _login(client, admin.email, "wrong-password")).status_code == 401
    await db_session.refresh(admin)
    assert admin.failed_login_count == 1
    assert admin.locked_until is None


async def test_success_resets_the_failure_count(client, db_session):
    admin = await _admin(db_session)
    await _login(client, admin.email, "wrong-password")
    await _login(client, admin.email, "wrong-password")
    assert (await _login(client, admin.email)).status_code == 200
    await db_session.refresh(admin)
    assert admin.failed_login_count == 0


async def test_login_limited_to_five_per_ip_counting_successes(client, db_session):
    admin = await _admin(db_session)
    ip = _ip()
    payload = {"email": admin.email, "password": PASSWORD}
    for _ in range(5):
        assert (await client.post(f"{AUTH}/login", json=payload, headers=ip)).status_code == 200
    r = await client.post(f"{AUTH}/login", json=payload, headers=ip)
    assert r.status_code == 429
    assert r.headers["Retry-After"]


async def test_login_limit_cannot_be_dodged_by_changing_the_client_sent_xff(client, db_session):
    """The proxy appends the real IP on the right; the client controls the left."""
    admin = await _admin(db_session)
    real_ip = _ip()["X-Forwarded-For"]
    payload = {"email": admin.email, "password": PASSWORD}
    for i in range(5):
        headers = {"X-Forwarded-For": f"1.2.3.{i}, {real_ip}"}
        assert (
            await client.post(f"{AUTH}/login", json=payload, headers=headers)
        ).status_code == 200
    headers = {"X-Forwarded-For": f"9.9.9.9, {real_ip}"}
    assert (await client.post(f"{AUTH}/login", json=payload, headers=headers)).status_code == 429


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


async def _challenge(client, admin) -> str:
    return (await _login(client, admin.email)).json()["challenge_token"]


async def test_setup_stores_secret_encrypted_and_does_not_activate(client, db_session):
    admin = await _admin(db_session)
    r = await client.post(
        f"{AUTH}/totp/setup", json={"challenge_token": await _challenge(client, admin)}
    )
    assert r.status_code == 200
    assert list(r.json()) == ["otpauth_url"]
    parsed = pyotp.parse_uri(r.json()["otpauth_url"])
    assert parsed.issuer == "EazyServ Admin"
    assert parsed.name == admin.email

    await db_session.refresh(admin)
    assert admin.totp_enabled_at is None
    assert admin.totp_secret != parsed.secret
    assert decrypt(admin.totp_secret) == parsed.secret


async def test_setup_again_before_activation_replaces_the_secret(client, db_session):
    admin = await _admin(db_session)
    challenge = await _challenge(client, admin)
    first = await client.post(f"{AUTH}/totp/setup", json={"challenge_token": challenge})
    second = await client.post(f"{AUTH}/totp/setup", json={"challenge_token": challenge})
    assert (
        pyotp.parse_uri(first.json()["otpauth_url"]).secret
        != pyotp.parse_uri(second.json()["otpauth_url"]).secret
    )


async def test_verify_activates_and_returns_a_session_that_works(client, db_session):
    admin = await _admin(db_session)
    challenge = await _challenge(client, admin)
    url = (await client.post(f"{AUTH}/totp/setup", json={"challenge_token": challenge})).json()[
        "otpauth_url"
    ]
    r = await client.post(
        f"{AUTH}/totp/verify",
        json={"challenge_token": challenge, "code": pyotp.parse_uri(url).now()},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 12 * 3600
    assert body["admin"]["email"] == admin.email
    assert body["admin"]["role"] == "admin"

    claims = decode_admin_token(body["access_token"], "admin")
    assert claims["typ"] == "admin"
    assert claims["sid"]
    assert "role" not in claims
    assert claims["exp"] - time.time() > 11.9 * 3600

    await db_session.refresh(admin)
    assert admin.totp_enabled_at is not None
    assert admin.last_login_at is not None

    me = await client.get(f"{AUTH}/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == admin.email


async def test_setup_refused_after_activation(client, db_session):
    admin = await _admin(db_session)
    await _set_up_totp(client, admin)
    r = await client.post(
        f"{AUTH}/totp/setup", json={"challenge_token": await _challenge(client, admin)}
    )
    assert r.status_code == 409
    assert r.json()["code"] == "TOTP_ALREADY_ACTIVE"


async def test_verify_before_setup_is_refused(client, db_session):
    admin = await _admin(db_session)
    r = await client.post(
        f"{AUTH}/totp/verify",
        json={"challenge_token": await _challenge(client, admin), "code": "123456"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "TOTP_SETUP_REQUIRED"


async def test_wrong_code_is_refused_and_does_not_activate(client, db_session):
    admin = await _admin(db_session)
    challenge = await _challenge(client, admin)
    url = (await client.post(f"{AUTH}/totp/setup", json={"challenge_token": challenge})).json()[
        "otpauth_url"
    ]
    good = pyotp.parse_uri(url).now()
    r = await client.post(
        f"{AUTH}/totp/verify",
        json={"challenge_token": challenge, "code": "000000" if good != "000000" else "111111"},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_TOTP_CODE"
    await db_session.refresh(admin)
    assert admin.totp_enabled_at is None


async def test_a_used_code_cannot_be_replayed_on_a_new_login(client, db_session):
    admin = await _admin(db_session)
    secret = await _set_up_totp(client, admin)
    code = pyotp.TOTP(secret).now()
    # _set_up_totp used "now" a moment ago; if the step rolled over, burn this one.
    first = await client.post(
        f"{AUTH}/totp/verify",
        json={"challenge_token": await _challenge(client, admin), "code": code},
    )
    assert first.status_code in (200, 401)
    r = await client.post(
        f"{AUTH}/totp/verify",
        json={"challenge_token": await _challenge(client, admin), "code": code},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_TOTP_CODE"


async def test_a_challenge_is_single_use(client, db_session):
    admin = await _admin(db_session)
    secret = await _set_up_totp(client, admin)
    challenge = await _challenge(client, admin)
    totp = pyotp.TOTP(secret)
    # The next step's code: valid inside the window, and not the one setup spent.
    next_code = totp.at(time.time() + 30)
    assert (
        await client.post(
            f"{AUTH}/totp/verify", json={"challenge_token": challenge, "code": next_code}
        )
    ).status_code == 200
    r = await client.post(
        f"{AUTH}/totp/verify",
        json={"challenge_token": challenge, "code": totp.at(time.time() - 30)},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CHALLENGE"


async def test_totp_attempts_limited_per_admin_even_across_new_logins(client, db_session):
    admin = await _admin(db_session)
    secret = await _set_up_totp(client, admin)  # one attempt
    bad = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"

    challenge = (await _login(client, admin.email)).json()["challenge_token"]
    for _ in range(9):
        r = await client.post(
            f"{AUTH}/totp/verify", json={"challenge_token": challenge, "code": bad}
        )
        assert r.status_code == 401

    fresh = (await _login(client, admin.email)).json()["challenge_token"]
    r = await client.post(
        f"{AUTH}/totp/verify",
        json={"challenge_token": fresh, "code": pyotp.TOTP(secret).at(time.time() + 30)},
    )
    assert r.status_code == 429
    assert r.headers["Retry-After"]


@pytest.mark.parametrize("path", ["totp/setup", "totp/verify"])
async def test_totp_endpoints_refuse_anything_but_a_live_challenge(client, db_session, path):
    admin = await _admin(db_session)
    bad_tokens = [
        "garbage",
        create_admin_session_token(admin.id, uuid.uuid4().hex),
        create_admin_challenge_token(admin.id, timedelta(seconds=-1)),
    ]
    for token in bad_tokens:
        r = await client.post(f"{AUTH}/{path}", json={"challenge_token": token, "code": "123456"})
        assert r.status_code == 401
        assert r.json()["code"] == "INVALID_CHALLENGE"


async def test_challenge_dies_when_the_account_is_deactivated(client, db_session):
    admin = await _admin(db_session)
    challenge = await _challenge(client, admin)
    admin.is_active = False
    await db_session.commit()
    r = await client.post(f"{AUTH}/totp/setup", json={"challenge_token": challenge})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


async def test_logout_ends_the_session_including_its_refreshed_token(client, db_session):
    admin = await _admin(db_session)
    sid = uuid.uuid4().hex
    old = _bearer(create_admin_session_token(admin.id, sid, timedelta(hours=1)))
    refreshed = (await client.get(f"{AUTH}/me", headers=old)).headers["X-Admin-Token"]

    r = await client.post(f"{AUTH}/logout", headers=_bearer(refreshed))
    assert r.status_code == 204
    for headers in (old, _bearer(refreshed)):
        assert (await client.get(f"{AUTH}/me", headers=headers)).status_code == 401
        assert (await client.get(f"{A}/users", headers=headers)).status_code == 401


async def test_logout_leaves_other_sessions_alone(client, db_session):
    admin = await _admin(db_session)
    other = _session(admin)
    assert (await client.post(f"{AUTH}/logout", headers=_session(admin))).status_code == 204
    assert (await client.get(f"{AUTH}/me", headers=other)).status_code == 200


async def test_young_token_gets_no_refresh(client, db_session):
    admin = await _admin(db_session)
    r = await client.get(f"{A}/users", headers=_session(admin, timedelta(hours=5)))
    assert r.status_code == 200
    assert "X-Admin-Token" not in r.headers


async def test_token_past_half_life_gets_a_fresh_one_with_the_same_sid(client, db_session):
    admin = await _admin(db_session)
    sid = uuid.uuid4().hex
    old = create_admin_session_token(admin.id, sid, timedelta(hours=5))
    r = await client.get(f"{A}/users", headers=_bearer(old))
    assert r.status_code == 200

    fresh = decode_admin_token(r.headers["X-Admin-Token"], "admin")
    assert fresh["sid"] == sid
    assert fresh["exp"] - time.time() > 11.9 * 3600
    # Refreshing doesn't kill the old token: parallel requests may still carry it.
    assert (await client.get(f"{AUTH}/me", headers=_bearer(old))).status_code == 200
    assert (await client.get(f"{AUTH}/me", headers=_bearer(old))).headers["X-Admin-Token"]


async def test_expired_session_is_refused(client, db_session):
    admin = await _admin(db_session)
    token = create_admin_session_token(admin.id, uuid.uuid4().hex, timedelta(seconds=-1))
    assert (await client.get(f"{A}/users", headers=_bearer(token))).status_code == 401


async def test_deactivating_an_admin_ends_their_live_session(client, db_session):
    admin = await _admin(db_session)
    headers = _session(admin)
    assert (await client.get(f"{A}/users", headers=headers)).status_code == 200
    admin.is_active = False
    await db_session.commit()
    assert (await client.get(f"{A}/users", headers=headers)).status_code == 401
    assert (await client.get(f"{AUTH}/me", headers=headers)).status_code == 401


async def test_role_is_read_from_the_db_on_every_request(client, db_session):
    admin = await _admin(db_session, AdminRole.ADMIN)
    headers = _session(admin)
    assert (await client.get(f"{A}/users", headers=headers)).status_code == 200
    admin.role = AdminRole.OPS
    await db_session.commit()
    assert (await client.get(f"{A}/users", headers=headers)).status_code == 403


# ---------------------------------------------------------------------------
# Tokens of the wrong kind
# ---------------------------------------------------------------------------


async def test_old_phone_login_admin_token_keeps_full_access(client, db_session, auth_headers):
    old_admin = await UserFactory.create(db_session, role="admin")
    headers = auth_headers(str(old_admin.id), "admin")
    assert (await client.get(f"{A}/users", headers=headers)).status_code == 200
    # An admin-only write: reaches the handler and fails on the body, not on auth.
    assert (
        await client.post(f"{A}/app-versions/android/release", json={}, headers=headers)
    ).status_code == 422


async def test_old_token_has_no_console_identity(client, db_session, auth_headers):
    old_admin = await UserFactory.create(db_session, role="admin")
    headers = auth_headers(str(old_admin.id), "admin")
    assert (await client.get(f"{AUTH}/me", headers=headers)).status_code == 401


@pytest.mark.parametrize("role", ["consumer", "provider"])
async def test_consumer_and_provider_tokens_get_403(client, db_session, auth_headers, role):
    user = await UserFactory.create(db_session, role=role)
    headers = auth_headers(str(user.id), role)
    assert (await client.get(f"{A}/users", headers=headers)).status_code == 403
    assert (await client.get(f"{AUTH}/me", headers=headers)).status_code == 401


async def test_forged_role_claim_on_a_consumer_token_gets_403(client, db_session, auth_headers):
    user = await UserFactory.create(db_session, role="consumer")
    r = await client.get(f"{A}/users", headers=auth_headers(str(user.id), "admin"))
    assert r.status_code == 403


async def test_challenge_token_is_not_a_session(client, db_session):
    admin = await _admin(db_session)
    headers = _bearer(await _challenge(client, admin))
    assert (await client.get(f"{A}/users", headers=headers)).status_code == 401
    assert (await client.get(f"{AUTH}/me", headers=headers)).status_code == 401
    assert (await client.post(f"{AUTH}/logout", headers=headers)).status_code == 401


async def test_no_token_is_401(client):
    assert (await client.get(f"{A}/users")).status_code == 401
    assert (await client.get(f"{AUTH}/me")).status_code == 401


async def test_admin_tokens_are_refused_everywhere_outside_admin(client, db_session):
    """Mainly the linked users row's id must not be reachable through ``sub``."""
    admin = await _admin(db_session)
    for token in (
        create_admin_session_token(admin.id, uuid.uuid4().hex),
        create_admin_challenge_token(admin.id),
    ):
        assert (await client.get("/api/v1/users/me", headers=_bearer(token))).status_code == 401
        r = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert r.status_code == 401


class _FakeWS:
    def __init__(self, token):
        self.query_params = {"token": token}
        self.closed = None

    async def close(self, code, reason):
        self.closed = code


async def test_admin_tokens_are_refused_by_the_websocket_check(db_session):
    admin = await _admin(db_session)
    ws = _FakeWS(create_admin_session_token(admin.id, uuid.uuid4().hex))
    assert await verify_ws_token(ws) is None
    assert ws.closed == 4001


# ---------------------------------------------------------------------------
# A refresh token only refreshes
# ---------------------------------------------------------------------------


def _refresh_headers(user) -> dict:
    return _bearer(
        create_refresh_token({"sub": str(user.id), "role": user.role.value, "type": "refresh"})
    )


@pytest.mark.parametrize("role", ["consumer", "provider", "admin"])
async def test_refresh_token_is_refused_on_normal_routes(client, db_session, role):
    user = await UserFactory.create(db_session, role=role)
    r = await client.get("/api/v1/users/me", headers=_refresh_headers(user))
    assert r.status_code == 401


async def test_refresh_token_is_refused_on_the_old_admin_path(client, db_session):
    old_admin = await UserFactory.create(db_session, role="admin")
    headers = _refresh_headers(old_admin)
    assert (await client.get(f"{A}/users", headers=headers)).status_code == 401
    # The release route's JWT branch, on the same check.
    r = await client.post(
        f"{A}/app-versions/android/release", json={"version": "9.9.9"}, headers=headers
    )
    assert r.status_code == 401


async def test_refresh_token_is_refused_by_the_websocket_check(db_session):
    user = await UserFactory.create(db_session, role="consumer")
    ws = _FakeWS(create_refresh_token({"sub": str(user.id), "role": "consumer", "type": "refresh"}))
    assert await verify_ws_token(ws) is None
    assert ws.closed == 4001


@pytest.mark.parametrize("role", ["consumer", "provider", "admin"])
async def test_access_token_still_works_on_normal_routes(client, db_session, auth_headers, role):
    user = await UserFactory.create(db_session, role=role)
    r = await client.get("/api/v1/users/me", headers=auth_headers(str(user.id), role))
    assert r.status_code == 200


async def test_access_token_still_works_on_the_websocket_check(db_session):
    user = await UserFactory.create(db_session, role="consumer")
    ws = _FakeWS(create_access_token({"sub": str(user.id), "role": "consumer"}))
    payload = await verify_ws_token(ws)
    assert payload["sub"] == str(user.id)
    assert ws.closed is None


async def test_refresh_endpoint_still_works_for_normal_users(client, db_session):
    user = await UserFactory.create(db_session, role="consumer")
    refresh = create_refresh_token({"sub": str(user.id), "role": "consumer", "type": "refresh"})
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Actor identity
# ---------------------------------------------------------------------------


async def test_linked_user_row_cannot_log_in_by_phone(client, db_session):
    admin = await _admin(db_session)
    r = await client.post(
        "/api/v1/auth/admin/send-otp", json={"phone": admin_placeholder_phone(admin.id)}
    )
    assert r.status_code == 422


async def test_routes_record_the_linked_user_as_actor(client, db_session):
    admin = await _admin(db_session)
    target = await UserFactory.create(db_session, role="consumer")
    r = await client.patch(
        f"{A}/users/{target.id}/deactivate", json={"reason": "test"}, headers=_session(admin)
    )
    assert r.status_code == 204
    action = (
        await db_session.execute(select(AdminAction).where(AdminAction.target_id == target.id))
    ).scalar_one()
    assert action.admin_user_id == admin.user_id


async def test_user_list_hides_linked_rows(client, db_session):
    admin = await _admin(db_session)
    r = await client.get(f"{A}/users", params={"per_page": 200}, headers=_session(admin))
    assert r.status_code == 200
    assert str(admin.user_id) not in {row["id"] for row in r.json()}


# ---------------------------------------------------------------------------
# Provisioning + password helpers
# ---------------------------------------------------------------------------


async def test_provision_refuses_duplicate_email_and_bad_passwords(db_session):
    admin = await _admin(db_session)
    with pytest.raises(ValueError, match="already"):
        await provision_admin(db_session, admin.email.upper(), AdminRole.OPS, PASSWORD)
    for bad in ("short", "x" * 73):
        with pytest.raises(ValueError, match="Password"):
            await provision_admin(db_session, "new@kalpai.in", AdminRole.OPS, bad)


async def test_provision_creates_an_inactive_linked_user(db_session):
    from app.models.user import User

    admin = await _admin(db_session)
    user = await db_session.get(User, admin.user_id)
    assert user.is_active is False
    assert user.phone == admin_placeholder_phone(admin.id)
    assert len(user.phone) == 15
    assert admin.password_hash.startswith("$2")


def test_password_hash_round_trip():
    hashed = hash_password(PASSWORD)
    assert verify_password(PASSWORD, hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("x" * 80, hashed)


# ---------------------------------------------------------------------------
# Release route: CI token path
# ---------------------------------------------------------------------------


async def test_release_route_ci_token_still_works(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "INTERNAL_RELEASE_TOKEN", "ci-token")
    r = await client.post(
        f"{A}/app-versions/android/release",
        json={},
        headers={"X-Internal-Release-Token": "ci-token"},
    )
    # Past auth: refused on the empty body.
    assert r.status_code == 422


async def test_release_route_without_any_auth_is_401(client):
    r = await client.post(f"{A}/app-versions/android/release", json={"version": "9.9.9"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Route table. A new /admin route must be added here, or this fails.
# ---------------------------------------------------------------------------

ALL = {"admin", "ops", "support"}
ADMIN_OPS = {"admin", "ops"}
ADMIN_SUPPORT = {"admin", "support"}
ADMIN_ONLY = {"admin"}

ROUTE_ROLES = {
    # admin.py
    ("GET", "/admin/users"): ADMIN_SUPPORT,
    ("PATCH", "/admin/users/{user_id}/deactivate"): ADMIN_ONLY,
    ("GET", "/admin/providers/kyc/pending"): ALL,
    ("GET", "/admin/providers/{user_id}/kyc"): ALL,
    ("POST", "/admin/providers/{user_id}/kyc/verify"): ADMIN_OPS,
    ("POST", "/admin/providers/{user_id}/kyc/reject"): ADMIN_OPS,
    ("GET", "/admin/pros/{professional_id}/kyc"): ALL,
    ("POST", "/admin/pros/{professional_id}/kyc/verify"): ADMIN_OPS,
    ("POST", "/admin/pros/{professional_id}/kyc/reject"): ADMIN_OPS,
    ("GET", "/admin/bookings"): ALL,
    ("POST", "/admin/bookings/{booking_id}/cancel"): ADMIN_OPS,
    ("GET", "/admin/bookings/{booking_id}"): ALL,
    ("GET", "/admin/bookings/{booking_id}/timeline"): ALL,
    ("GET", "/admin/providers/pending-settlements"): ADMIN_SUPPORT,
    ("GET", "/admin/providers/{user_id}/payouts"): ADMIN_SUPPORT,
    ("POST", "/admin/providers/{user_id}/payout"): ADMIN_ONLY,
    ("POST", "/admin/providers/{user_id}/payout/requeue-failed"): ADMIN_ONLY,
    ("POST", "/admin/providers/{user_id}/payout/requeue-reversed"): ADMIN_ONLY,
    ("POST", "/admin/app-versions/{platform}/release"): ADMIN_ONLY,
    # admin_team.py
    ("GET", "/admin/providers"): ALL,
    ("GET", "/admin/providers/{sp_user_id}"): ALL,
    ("GET", "/admin/providers/{sp_user_id}/team"): ALL,
    ("GET", "/admin/providers/{sp_user_id}/invites"): ALL,
    ("GET", "/admin/pros/{professional_id}"): ALL,
    ("POST", "/admin/pros/{professional_id}/force-remove"): ADMIN_ONLY,
    ("POST", "/admin/pros/{professional_id}/reassign"): ADMIN_ONLY,
    ("GET", "/admin/actions"): ADMIN_SUPPORT,
    # services.py: admin writes outside the /admin prefix
    ("POST", "/services"): ADMIN_OPS,
    ("PATCH", "/services/{service_id}"): ADMIN_OPS,
    ("DELETE", "/services/{service_id}"): ADMIN_OPS,
}


def test_route_table_covers_every_admin_route():
    prefix = get_settings().API_V1_PREFIX
    found = set()
    for route in create_app().routes:
        path = getattr(route, "path", "")
        if not path.startswith(f"{prefix}/admin") or path.startswith(f"{prefix}/admin/auth"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            found.add((method, path.removeprefix(prefix)))
    table = {key for key in ROUTE_ROLES if key[1].startswith("/admin")}
    assert found == table


def _concrete(path: str) -> str:
    out = path
    for name in ("user_id", "professional_id", "booking_id", "sp_user_id", "service_id"):
        out = out.replace("{" + name + "}", str(uuid.uuid4()))
    return out.replace("{platform}", "android")


# Every call sends an empty JSON body, so a write with a required body stops at 422
# without side effects. These have no body, so an allowed role runs the handler
# with an unknown id: they return an empty result or a 404, and change nothing.
@pytest.mark.parametrize(("method", "path"), sorted(ROUTE_ROLES))
async def test_route_gating(client, db_session, auth_headers, method, path):
    url = f"/api/v1{_concrete(path)}"
    body = {"json": {}} if method in ("POST", "PATCH") else {}
    for role in AdminRole:
        admin = await provision_admin(
            db_session, f"g{uuid.uuid4().hex[:10]}@kalpai.in", role, PASSWORD
        )
        await db_session.commit()
        r = await client.request(method, url, headers=_session(admin), **body)
        if role.value in ROUTE_ROLES[(method, path)]:
            assert r.status_code not in (401, 403), f"{role.value}: {r.status_code} {r.text}"
        else:
            assert r.status_code == 403, f"{role.value}: {r.status_code}"

    # An old phone-login admin token passes every route; a consumer passes none.
    old_admin = await UserFactory.create(db_session, role="admin")
    r = await client.request(method, url, headers=auth_headers(str(old_admin.id), "admin"), **body)
    assert r.status_code not in (401, 403), f"old admin: {r.status_code} {r.text}"
    consumer = await UserFactory.create(db_session, role="consumer")
    r = await client.request(method, url, headers=auth_headers(str(consumer.id)), **body)
    assert r.status_code == 403


async def test_refreshed_token_header_comes_back_on_any_gated_route(client, db_session):
    admin = await _admin(db_session)
    old = _session(admin, timedelta(hours=7))
    r = await client.get(f"{A}/providers", headers=old)
    assert r.status_code == 200
    assert r.headers["X-Admin-Token"]
