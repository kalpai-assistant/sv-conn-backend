"""
In-app account deletion (SVC-367): ``DELETE /users/me``.

The users row is tombstoned, not deleted: nothing that references it may
break, so every test that deletes also reads a past booking back afterwards.
The refusals must change nothing, so each one checks the user is untouched.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.professional import Professional
from app.models.provider_ledger_entry import LedgerEntryType, ProviderLedgerEntry
from app.models.provider_profile import ProviderProfile
from app.models.provider_team_invite import InviteStatus, ProviderTeamInvite
from app.models.user import User
from app.services.account_service import tombstone_phone
from app.services.provider_service import ProviderService
from app.utils import storage
from app.utils.otp import store_otp
from app.utils.security import create_refresh_token, decode_jwt
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_two_level_dispatch import (
    _as,
    _assigned_to,
    _team_pro,
    _team_sp,
    pushes,  # noqa: F401  (fixture)
)

ME = "/api/v1/users/me"
BOOKING = "/api/v1/bookings/{}"
TIMELINE = "/api/v1/admin/bookings/{}/timeline"
BUCKET_URL = f"https://{storage.settings.S3_BUCKET_NAME}.s3.amazonaws.com/"

pytestmark = pytest.mark.usefixtures("pushes")


@pytest.fixture(autouse=True)
def deleted_keys(monkeypatch):
    """Every test stubs S3; this records the named keys deleted."""
    keys: list[str] = []
    monkeypatch.setattr(storage, "delete_file", keys.append)
    return keys


@pytest.fixture(autouse=True)
def swept_prefixes(monkeypatch):
    """The folders swept for objects the rows no longer name."""
    prefixes: list[str] = []

    def sweep(prefix):
        prefixes.append(prefix)
        return 0

    monkeypatch.setattr(storage, "delete_prefix", sweep)
    return prefixes


async def _delete(client, auth_headers, user, role, **kwargs):
    return await client.request("DELETE", ME, headers=_as(auth_headers, user, role), **kwargs)


async def _completed_booking(db, sp, pro=None):
    consumer, booking = await _booking(db, provider_id=sp.id, status=BookingStatus.COMPLETED)
    if pro is not None:
        booking.professional_id = pro.id
        await db.flush()
    return consumer, booking


async def _invite(db, sp_id, phone, status=InviteStatus.PENDING):
    invite = ProviderTeamInvite(
        invited_by=sp_id,
        invitee_phone=phone,
        token=f"{uuid.uuid4().int % 10**6:06d}",
        status=status.value,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(invite)
    await db.flush()
    return invite


async def _assert_past_booking_serialises(client, auth_headers, db, booking, viewer, role):
    resp = await client.get(BOOKING.format(booking.id), headers=_as(auth_headers, viewer, role))
    assert resp.status_code == 200, resp.text
    admin = await UserFactory.create(db, role="admin")
    resp_t = await client.get(
        TIMELINE.format(booking.id), headers=_as(auth_headers, admin, "admin")
    )
    assert resp_t.status_code == 200, resp_t.text
    return resp.json()


# --- Consumer ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_is_tombstoned_and_their_past_booking_still_reads(
    client, db_session, auth_headers, redis_client, deleted_keys, swept_prefixes
):
    sp = await _eligible_provider(db_session, await _service(db_session))
    consumer, booking = await _completed_booking(db_session, sp)
    consumer.email = "a@example.com"
    consumer.fcm_token = "tok"
    address = await db_session.get(Address, booking.consumer_address_id)
    address.house_floor, address.contact_phone = "Flat 3", "+919800000000"
    await db_session.flush()
    old_phone = consumer.phone
    refresh = create_refresh_token({"sub": str(consumer.id), "role": "consumer", "type": "refresh"})

    resp = await _delete(
        client, auth_headers, consumer, "consumer", json={"refresh_token": refresh}
    )

    assert resp.status_code == 204, resp.text
    await db_session.refresh(consumer)
    assert consumer.phone == tombstone_phone(consumer.id) != old_phone
    assert len(consumer.phone) <= 15
    assert (consumer.name, consumer.email, consumer.fcm_token) == (None, None, None)
    assert consumer.is_active is False and consumer.deleted_at is not None
    await db_session.refresh(address)
    assert address.full_address == "Deleted address"
    assert (address.house_floor, address.contact_phone, address.location) == (None, None, None)
    assert address.deleted_at is not None
    assert deleted_keys == []
    # A consumer has no S3 folders.
    assert swept_prefixes == []

    # The refresh token is revoked, and the access token no longer works.
    assert await redis_client.exists(f"jwt:blacklist:{decode_jwt(refresh)['jti']}")
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    ).status_code == 401
    assert (await _delete(client, auth_headers, consumer, "consumer")).status_code == 401

    # The booking and its FKs are intact; the provider still sees it.
    await db_session.refresh(booking)
    assert booking.consumer_address_id == address.id and booking.consumer_id == consumer.id
    body = await _assert_past_booking_serialises(
        client, auth_headers, db_session, booking, sp, "provider"
    )
    assert body["id"] == str(booking.id)


@pytest.mark.asyncio
async def test_same_phone_signs_up_again_as_a_new_account(
    client, db_session, auth_headers, redis_client
):
    phone = f"+9198{uuid.uuid4().int % 10**8:08d}"
    user = await UserFactory.create(db_session, role="consumer", phone=phone)
    assert (await _delete(client, auth_headers, user, "consumer")).status_code == 204

    await store_otp(redis_client, phone, "123456")
    resp = await client.post(
        "/api/v1/auth/verify-otp", json={"phone": phone, "otp": "123456", "role": "provider"}
    )

    # A fresh account: new id, new user, and no role carried over (SVC-253).
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_new_user"] is True
    assert resp.json()["user_id"] != str(user.id)
    assert resp.json()["role"] == "provider"


# --- SP ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_solo_sp_is_scrubbed_and_its_s3_objects_go(
    client, db_session, auth_headers, deleted_keys, swept_prefixes
):
    sp = await _eligible_provider(db_session, await _service(db_session))
    self_pro = await ProviderService(db_session).ensure_self_professional(sp.id)
    profile = await db_session.scalar(
        select(ProviderProfile).where(ProviderProfile.user_id == sp.id)
    )
    profile.business_name = "Sharma Cleaners"
    profile.profile_photo_url = f"{BUCKET_URL}provider-photos/{sp.id}/a.jpg"
    profile.business_image_url = f"{BUCKET_URL}provider-photos/{sp.id}/business-b.png"
    profile.payout_vpa_encrypted = "enc"
    profile.pan_encrypted = "enc"
    profile.payout_display = "sharma@upi"
    profile.kyc_docs = {
        "aadhaar_front_key": f"kyc/{sp.id}/f.jpg",
        "aadhaar_back_key": f"kyc/{sp.id}/b.jpg",
        "selfie_key": f"kyc/{sp.id}/s.jpg",
        "aadhaar_front_content_type": "image/jpeg",
    }
    _, booking = await _completed_booking(db_session, sp, self_pro)
    # Paid out already: a credit and a matching payout debit net to zero.
    for amount, kind in (
        (8000, LedgerEntryType.BOOKING_COMPLETED),
        (-8000, LedgerEntryType.PAYOUT),
    ):
        db_session.add(ProviderLedgerEntry(provider_id=sp.id, amount_paise=amount, entry_type=kind))
    sent = await _invite(db_session, sp.id, f"+9197{uuid.uuid4().int % 10**8:08d}")
    await db_session.flush()

    resp = await _delete(client, auth_headers, sp, "provider")

    assert resp.status_code == 204, resp.text
    await db_session.refresh(profile)
    assert profile.business_name == "Deleted provider"
    assert profile.business_image_url is None and profile.profile_photo_url is None
    assert profile.payout_vpa_encrypted is None and profile.pan_encrypted is None
    assert profile.payout_display is None and profile.kyc_docs is None
    assert profile.is_online is False
    await db_session.refresh(self_pro)
    assert (self_pro.is_active, self_pro.display_name) == (False, "Deleted professional")
    await db_session.refresh(sent)
    assert sent.status == InviteStatus.REVOKED.value
    assert sorted(deleted_keys) == sorted(
        [
            f"provider-photos/{sp.id}/a.jpg",
            f"provider-photos/{sp.id}/business-b.png",
            f"kyc/{sp.id}/f.jpg",
            f"kyc/{sp.id}/b.jpg",
            f"kyc/{sp.id}/s.jpg",
        ]
    )
    # Plus the folders, for photos replaced by a re-upload and a redone KYC.
    assert sorted(swept_prefixes) == sorted(
        [f"provider-photos/{sp.id}/", f"kyc/{sp.id}/", f"kyc/pros/{self_pro.id}/"]
    )

    # The consumer still sees the past job, now under the placeholder name.
    consumer = await db_session.get(User, booking.consumer_id)
    await _assert_past_booking_serialises(
        client, auth_headers, db_session, booking, consumer, "consumer"
    )


@pytest.mark.asyncio
async def test_s3_failure_still_deletes_the_account(client, db_session, auth_headers, monkeypatch):
    sp = await _eligible_provider(db_session, await _service(db_session))
    profile = await db_session.scalar(
        select(ProviderProfile).where(ProviderProfile.user_id == sp.id)
    )
    profile.profile_photo_url = f"{BUCKET_URL}provider-photos/{sp.id}/a.jpg"
    await db_session.flush()

    def boom(key):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(storage, "delete_file", boom)
    monkeypatch.setattr(storage, "delete_prefix", boom)

    assert (await _delete(client, auth_headers, sp, "provider")).status_code == 204
    await db_session.refresh(sp)
    assert sp.is_active is False


def test_a_sweep_failure_does_not_stop_the_named_deletes(monkeypatch, deleted_keys):
    """No s3:ListBucket on the role: the named keys still go."""
    from app.services.account_service import S3Cleanup, delete_objects

    def denied(prefix):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(storage, "delete_prefix", denied)
    delete_objects(uuid.uuid4(), S3Cleanup(keys=["a", "b"], prefixes=["x/", "y/"]))
    assert deleted_keys == ["a", "b"]


class _FakeS3:
    """Two pages under the prefix; records what delete_objects was sent."""

    def __init__(self, pages):
        self.pages, self.deleted, self.listed = pages, [], []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):  # noqa: N803 (boto3's names)
        self.listed.append(Prefix)
        return [{"Contents": [{"Key": k} for k in page]} if page else {} for page in self.pages]

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        self.deleted += [o["Key"] for o in Delete["Objects"]]
        return {}


def test_delete_prefix_removes_every_page(monkeypatch):
    monkeypatch.undo()  # the real delete_prefix, not the autouse stub
    fake = _FakeS3([["p/1.jpg", "p/2.jpg"], [], ["p/3.jpg"]])
    monkeypatch.setattr(storage, "_get_client", lambda: fake)

    assert storage.delete_prefix("p/") == 3
    assert fake.listed == ["p/"]
    assert fake.deleted == ["p/1.jpg", "p/2.jpg", "p/3.jpg"]


@pytest.mark.parametrize("prefix", ["", "/", "provider-photos"])
def test_delete_prefix_refuses_anything_but_a_folder(monkeypatch, prefix):
    monkeypatch.undo()
    monkeypatch.setattr(storage, "_get_client", lambda: pytest.fail("S3 was called"))
    with pytest.raises(ValueError):
        storage.delete_prefix(prefix)


@pytest.mark.asyncio
async def test_sp_owed_money_is_refused(client, db_session, auth_headers):
    sp = await _eligible_provider(db_session, await _service(db_session))
    db_session.add(
        ProviderLedgerEntry(
            provider_id=sp.id, amount_paise=8000, entry_type=LedgerEntryType.BOOKING_COMPLETED
        )
    )
    await db_session.flush()

    resp = await _delete(client, auth_headers, sp, "provider")

    assert resp.status_code == 409
    assert resp.json()["code"] == "UNPAID_BALANCE"
    assert resp.json()["details"] == {"balance_paise": 8000}
    await db_session.refresh(sp)
    assert sp.is_active is True and sp.deleted_at is None


@pytest.mark.asyncio
async def test_sp_owing_money_can_still_delete(client, db_session, auth_headers, deleted_keys):
    sp = await _eligible_provider(db_session, await _service(db_session))
    db_session.add(
        ProviderLedgerEntry(
            provider_id=sp.id, amount_paise=-500, entry_type=LedgerEntryType.CASH_COMMISSION
        )
    )
    await db_session.flush()

    assert (await _delete(client, auth_headers, sp, "provider")).status_code == 204


@pytest.mark.asyncio
async def test_sp_with_a_team_is_refused(client, db_session, auth_headers):
    sp, _, member = await _team_sp(db_session, await _service(db_session))

    resp = await _delete(client, auth_headers, sp, "provider")

    assert resp.status_code == 409
    assert resp.json()["code"] == "TEAM_HAS_PROS"
    assert resp.json()["details"] == {"professional_ids": [str(member.id)]}
    await db_session.refresh(sp)
    assert sp.is_active is True


# --- Team Pro ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_pro_leaves_the_team_and_their_past_job_still_reads(
    client, db_session, auth_headers, deleted_keys, swept_prefixes
):
    sp, self_pro, member = await _team_sp(db_session, await _service(db_session))
    # The SP is off tools; losing its only team Pro must switch it back on (SVC-326).
    self_pro.accepts_direct_jobs = False
    member.kyc_docs = {"selfie_key": f"kyc/pros/{member.id}/s.jpg"}
    _, booking = await _completed_booking(db_session, sp, member)
    member_user = await db_session.get(User, member.user_id)
    accepted = await _invite(db_session, sp.id, member_user.phone, status=InviteStatus.ACCEPTED)

    resp = await _delete(client, auth_headers, member_user, "consumer")

    assert resp.status_code == 204, resp.text
    await db_session.refresh(member)
    assert (member.is_active, member.is_online) == (False, False)
    assert member.display_name == "Deleted professional" and member.kyc_docs is None
    await db_session.refresh(self_pro)
    assert self_pro.accepts_direct_jobs is True
    await db_session.refresh(accepted)
    assert accepted.invitee_phone == tombstone_phone(member_user.id)
    assert accepted.status == InviteStatus.ACCEPTED.value
    assert deleted_keys == [f"kyc/pros/{member.id}/s.jpg"]
    # A team Pro has no SP folders; only their own KYC folder is swept.
    assert swept_prefixes == [f"kyc/pros/{member.id}/"]

    await db_session.refresh(booking)
    assert booking.professional_id == member.id
    await _assert_past_booking_serialises(client, auth_headers, db_session, booking, sp, "provider")


@pytest.mark.asyncio
async def test_pending_invite_to_the_deleted_phone_is_revoked(client, db_session, auth_headers):
    sp = await _eligible_provider(db_session, await _service(db_session))
    invitee = await UserFactory.create(db_session, role="consumer")
    invite = await _invite(db_session, sp.id, invitee.phone)

    assert (await _delete(client, auth_headers, invitee, "consumer")).status_code == 204

    await db_session.refresh(invite)
    assert invite.status == InviteStatus.REVOKED.value
    assert invite.revoked_at is not None
    assert invite.invitee_phone == tombstone_phone(invitee.id)


# --- Live bookings block it ----------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_with_a_requested_booking_is_refused(client, db_session, auth_headers):
    consumer, booking = await _booking(db_session)

    resp = await _delete(client, auth_headers, consumer, "consumer")

    assert resp.status_code == 409
    assert resp.json()["code"] == "ACTIVE_BOOKINGS"
    assert resp.json()["details"] == {"booking_ids": [str(booking.id)]}
    await db_session.refresh(consumer)
    assert consumer.is_active is True and consumer.name is not None


@pytest.mark.asyncio
async def test_sp_holding_a_job_is_refused(client, db_session, auth_headers):
    sp = await _eligible_provider(db_session, await _service(db_session))
    _, booking = await _booking(db_session, provider_id=sp.id, status=BookingStatus.EN_ROUTE)

    resp = await _delete(client, auth_headers, sp, "provider")

    assert resp.status_code == 409
    assert resp.json()["details"]["booking_ids"] == [str(booking.id)]


@pytest.mark.asyncio
async def test_team_pro_assigned_to_a_job_is_refused(client, db_session, auth_headers):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)
    member_user = await db_session.get(User, member.user_id)

    resp = await _delete(client, auth_headers, member_user, "consumer")

    assert resp.status_code == 409
    assert resp.json()["code"] == "ACTIVE_BOOKINGS"
    assert resp.json()["details"]["booking_ids"] == [str(booking.id)]
    await db_session.refresh(member)
    assert member.is_active is True


@pytest.mark.asyncio
async def test_a_removed_pro_row_is_ignored_for_team_refusal(client, db_session, auth_headers):
    """Only active team Pros block: a member the SP already removed doesn't."""
    sp = await _eligible_provider(db_session, await _service(db_session))
    await ProviderService(db_session).ensure_self_professional(sp.id)
    await _team_pro(db_session, sp.id, active=False)

    assert (await _delete(client, auth_headers, sp, "provider")).status_code == 204
    rows = (
        await db_session.scalars(
            select(Professional).where(Professional.service_provider_id == sp.id)
        )
    ).all()
    assert len(rows) == 2
