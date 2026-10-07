"""
SVC-320 PR 3: the Pro's own surface (``/pros/me/*``), Pro KYC (SVC-327), the
admin KYC queue's SP-vs-Pro rows, and the access rules that let the assigned Pro
work a job — plus the consumer-guard fix that came with it.

Team Pros here are consumer-role accounts, the way invitees really sign up.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import inspect as sa_inspect

from app.models.booking import BookingStatus, PaymentMethod
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus
from app.services.booking_service import BookingService
from app.services.provider_service import ProviderService
from app.tasks import notification_tasks
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_kyc import _kyc_files

ME = "/api/v1/pros/me"
JOBS = "/api/v1/pros/me/jobs"
UPLOAD = "app.services.provider_service.storage.upload_file"


@pytest.fixture
def pushes():
    sent: list[str] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append(kw["event"]),
    ):
        yield sent


@pytest.fixture
def cash_task():
    with patch("app.routers.bookings.process_cash_payment") as task:
        yield task


def _as(auth_headers, user):
    return auth_headers(str(user.id), user.role.value)


async def _team(db, *, business_name="Sparkle Cleaners"):
    """An eligible SP (with its Pro-for-self) and one online team Pro."""
    service = await _service(db)
    sp = await _eligible_provider(db, service)
    profile = await ProviderService(db).get_profile(sp.id)
    profile.business_name = business_name
    self_pro = await ProviderService(db).ensure_self_professional(sp.id)
    member_user = await UserFactory.create(db, role="consumer", name="Rohan")
    member = Professional(
        service_provider_id=sp.id,
        user_id=member_user.id,
        display_name="Rohan",
        is_online=True,
    )
    db.add(member)
    await db.flush()
    return service, sp, self_pro, member, member_user


async def _assigned_job(db, service, sp, member, *, cash=False):
    consumer, booking = await _booking(db, service=service)
    if cash:
        booking.payment_method = PaymentMethod.CASH
    # /assign needs a team Pro's own KYC verified. ``_team`` leaves it pending on
    # purpose, for the KYC tests, so verify it here, where a job is assigned.
    member.kyc_status = KYCStatus.VERIFIED
    await db.flush()
    booking, _, _ = await BookingService(db).assign(booking.id, sp.id, member.id)
    return consumer, booking


# --- GET /pros/me ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_team_pro_who_is_a_consumer_sees_their_profile(client, db_session, auth_headers):
    _, sp, _, member, member_user = await _team(db_session)

    resp = await client.get(ME, headers=_as(auth_headers, member_user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["id"], body["display_name"], body["is_self"]) == (str(member.id), "Rohan", False)
    assert body["is_online"] is True
    assert body["kyc_status"] == "pending", "their own KYC, not the SP's"
    assert body["service_provider"] == {
        "user_id": str(sp.id),
        "business_name": "Sparkle Cleaners",
        "business_image_url": None,
    }


@pytest.mark.asyncio
async def test_not_a_pro_is_404(client, db_session, auth_headers):
    stranger = await UserFactory.create(db_session, role="consumer")
    resp = await client.get(ME, headers=_as(auth_headers, stranger))
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_A_PRO"


@pytest.mark.asyncio
async def test_a_removed_pro_loses_the_surface(client, db_session, auth_headers):
    _, _, _, member, member_user = await _team(db_session)
    member.is_active = False
    await db_session.flush()
    assert (await client.get(ME, headers=_as(auth_headers, member_user))).status_code == 404


@pytest.mark.asyncio
async def test_a_solo_sp_is_their_own_pro_with_the_sp_kyc(client, db_session, auth_headers):
    _, sp, self_pro, _, _ = await _team(db_session)
    profile = await ProviderService(db_session).get_profile(sp.id)
    profile.kyc_status = KYCStatus.VERIFIED
    self_pro.kyc_status = KYCStatus.PENDING  # a stale SVC-339 copy
    await db_session.flush()

    body = (await client.get(ME, headers=_as(auth_headers, sp))).json()
    assert body["is_self"] is True
    assert body["kyc_status"] == "verified", "the SP's KYC, not the stale copy"


# --- SVC-327 item 2: no payout details on a Pro ------------------------------------


@pytest.mark.asyncio
async def test_no_pro_surface_carries_payout_details(client, db_session, auth_headers):
    payout_words = ("payout", "upi", "vpa", "bank", "ifsc", "account")
    _, _, _, _, member_user = await _team(db_session)
    body = (await client.get(ME, headers=_as(auth_headers, member_user))).json()
    flat = str(body).lower()
    assert not any(word in flat for word in payout_words)

    columns = {c.key for c in sa_inspect(Professional).columns}
    assert not any(
        word in c for c in columns for word in payout_words
    ), "payout details belong to the SP row only"
    schemas = client._transport.app.openapi()["components"]["schemas"]
    for name in ("ProMeResponse", "ProStatusResponse", "ProfessionalResponse"):
        assert not any(
            word in field for field in schemas[name]["properties"] for word in payout_words
        )
    paths = client._transport.app.openapi()["paths"]
    assert not any("payout" in path for path in paths if path.startswith("/api/v1/pros"))


# --- Online + location ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_team_pro_toggles_their_own_online(client, db_session, auth_headers):
    _, sp, self_pro, member, member_user = await _team(db_session)
    resp = await client.post(
        f"{ME}/online", json={"is_online": False}, headers=_as(auth_headers, member_user)
    )
    assert resp.status_code == 200
    assert resp.json()["is_online"] is False
    await db_session.refresh(member)
    assert member.is_online is False
    profile = await ProviderService(db_session).get_profile(sp.id)
    assert profile.is_online is True, "a team Pro's switch never moves the SP's"


@pytest.mark.asyncio
async def test_the_sp_toggle_and_the_pro_for_self_move_together(client, db_session, auth_headers):
    _, sp, self_pro, _, _ = await _team(db_session)

    await client.post(f"{ME}/online", json={"is_online": False}, headers=_as(auth_headers, sp))
    profile = await ProviderService(db_session).get_profile(sp.id)
    await db_session.refresh(self_pro)
    assert (profile.is_online, self_pro.is_online) == (False, False)

    resp = await client.patch(
        "/api/v1/providers/me/status", json={"is_online": True}, headers=_as(auth_headers, sp)
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(self_pro)
    assert self_pro.is_online is True


@pytest.mark.asyncio
async def test_location_heartbeat_is_validated_and_rate_limited(client, db_session, auth_headers):
    _, _, _, member, member_user = await _team(db_session)
    headers = _as(auth_headers, member_user)

    resp = await client.post(f"{ME}/location", json={"lat": 18.52, "lng": 73.85}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_location_at"] is not None
    await db_session.refresh(member)
    assert member.last_location is not None

    for bad in ({"lat": 91, "lng": 0}, {"lat": 0, "lng": -181}):
        assert (await client.post(f"{ME}/location", json=bad, headers=headers)).status_code == 422

    for _ in range(7):  # 3 calls so far, all counted
        await client.post(f"{ME}/location", json={"lat": 18.5, "lng": 73.8}, headers=headers)
    resp = await client.post(f"{ME}/location", json={"lat": 18.5, "lng": 73.8}, headers=headers)
    assert resp.status_code == 429


# --- Jobs: list, view, work -----------------------------------------------------------


@pytest.mark.asyncio
async def test_my_jobs_and_the_job_view(client, db_session, auth_headers, pushes):
    service, sp, _, member, member_user = await _team(db_session)
    _, booking = await _assigned_job(db_session, service, sp, member)
    headers = _as(auth_headers, member_user)

    active = (await client.get(JOBS, headers=headers)).json()
    assert [b["id"] for b in active["bookings"]] == [str(booking.id)]
    assert active["total"] == 1
    assert active["bookings"][0]["doorstep_otp"] is None
    assert active["bookings"][0]["address"] is not None
    assert (await client.get(JOBS, params={"scope": "past"}, headers=headers)).json()["total"] == 0

    # One booking view: GET /bookings/{id} admits the assigned Pro.
    resp = await client.get(f"/api/v1/bookings/{booking.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["address"] is not None and resp.json()["doorstep_otp"] is None


@pytest.mark.asyncio
async def test_a_team_pro_works_the_job_end_to_end(
    client, db_session, auth_headers, pushes, cash_task
):
    service, sp, _, member, member_user = await _team(db_session)
    _, booking = await _assigned_job(db_session, service, sp, member, cash=True)
    headers = _as(auth_headers, member_user)
    pushes.clear()

    resp = await client.post(f"{JOBS}/{booking.id}/en-route", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "en_route"

    not_it = "0000" if booking.doorstep_otp != "0000" else "1111"
    wrong = await client.post(f"{JOBS}/{booking.id}/start", json={"otp": not_it}, headers=headers)
    assert wrong.status_code == 400, "the same doorstep-OTP check as the provider route"

    resp = await client.post(
        f"{JOBS}/{booking.id}/start", json={"otp": booking.doorstep_otp}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_progress"

    resp = await client.post(f"{JOBS}/{booking.id}/complete", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"

    assert pushes == ["en_route", "in_progress", "completed"], "the same SVC-334 pushes"
    cash_task.delay.assert_called_once_with(booking_id=str(booking.id))
    past = (await client.get(JOBS, params={"scope": "past"}, headers=headers)).json()
    assert [b["id"] for b in past["bookings"]] == [str(booking.id)]


@pytest.mark.asyncio
async def test_only_the_assigned_active_pro_works_the_job(client, db_session, auth_headers, pushes):
    service, sp, _, member, member_user = await _team(db_session)
    colleague_user = await UserFactory.create(db_session, role="consumer")
    db_session.add(
        Professional(service_provider_id=sp.id, user_id=colleague_user.id, display_name="Other")
    )
    await db_session.flush()
    _, booking = await _assigned_job(db_session, service, sp, member)

    resp = await client.post(
        f"{JOBS}/{booking.id}/en-route", headers=_as(auth_headers, colleague_user)
    )
    assert resp.status_code == 404
    resp = await client.get(
        f"/api/v1/bookings/{booking.id}", headers=_as(auth_headers, colleague_user)
    )
    assert resp.status_code == 403

    member.is_active = False
    await db_session.flush()
    headers = _as(auth_headers, member_user)
    assert (await client.post(f"{JOBS}/{booking.id}/en-route", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/bookings/{booking.id}", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_the_sp_keeps_its_own_routes_on_a_team_job(client, db_session, auth_headers, pushes):
    service, sp, _, member, _ = await _team(db_session)
    _, booking = await _assigned_job(db_session, service, sp, member)
    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/en-route", headers=_as(auth_headers, sp)
    )
    assert resp.status_code == 200, resp.text
    assert "en_route" in pushes


@pytest.mark.asyncio
async def test_a_solo_sp_works_their_job_through_either_surface(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/accept", headers=_as(auth_headers, sp)
    )
    assert resp.status_code == 200
    resp = await client.post(f"{JOBS}/{booking.id}/en-route", headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text


# --- /assign online check -------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_refuses_an_offline_team_pro_but_not_the_sp_itself(
    client, db_session, auth_headers, pushes
):
    service, sp, self_pro, member, _ = await _team(db_session)
    member.kyc_status = KYCStatus.VERIFIED  # so the online check is what answers
    member.is_online = False
    self_pro.is_online = False
    await db_session.flush()
    _, booking = await _booking(db_session, service=service)

    resp = await client.post(
        f"/api/v1/bookings/{booking.id}/assign",
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "PRO_OFFLINE"

    resp = await client.post(
        f"/api/v1/bookings/{booking.id}/assign",
        json={"professional_id": str(self_pro.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 200, resp.text


# --- Pro KYC + the admin queue --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_team_pro_submits_kyc_onto_their_own_row(client, db_session, auth_headers):
    _, sp, _, member, member_user = await _team(db_session)
    profile = await ProviderService(db_session).get_profile(sp.id)
    sp_before = (profile.kyc_status, profile.kyc_docs)

    with patch(UPLOAD) as upload:
        resp = await client.post(
            f"{ME}/kyc", files=_kyc_files(), headers=_as(auth_headers, member_user)
        )

    assert resp.status_code == 201, resp.text
    assert upload.call_count == 3
    assert all(c.args[1].startswith(f"kyc/pros/{member.id}/") for c in upload.call_args_list)
    await db_session.refresh(member)
    assert member.kyc_status is KYCStatus.SUBMITTED
    assert member.kyc_docs["selfie_key"].startswith(f"kyc/pros/{member.id}/")
    await db_session.refresh(profile)
    assert (profile.kyc_status, profile.kyc_docs) == sp_before, "the SP row is untouched"


@pytest.mark.asyncio
async def test_a_pro_for_self_submits_kyc_as_the_sp(client, db_session, auth_headers):
    _, sp, _, _, _ = await _team(db_session)
    with patch(UPLOAD):
        resp = await client.post(f"{ME}/kyc", files=_kyc_files(), headers=_as(auth_headers, sp))
    assert resp.status_code == 409
    assert resp.json()["code"] == "SELF_PRO_KYC_IS_SP_KYC"


async def _admin(db):
    return await UserFactory.create(db, role="admin")


@pytest.mark.asyncio
async def test_the_admin_queue_is_sp_only_unless_asked(client, db_session, auth_headers):
    _, sp, self_pro, member, member_user = await _team(db_session, business_name="Zayn Co")
    profile = await ProviderService(db_session).get_profile(sp.id)
    profile.kyc_status = KYCStatus.SUBMITTED
    self_pro.kyc_status = KYCStatus.SUBMITTED  # the SVC-339 copy: never listed as a Pro
    member.kyc_status = KYCStatus.SUBMITTED
    await db_session.flush()
    headers = _as(auth_headers, await _admin(db_session))
    queue = "/api/v1/admin/providers/kyc/pending"

    default = (await client.get(queue, headers=headers)).json()
    assert {r["kind"] for r in default} == {"sp"}
    assert str(member_user.id) not in {r["id"] for r in default}
    assert str(sp.id) in {r["id"] for r in default}, "SP rows keep id = the SP's user id"

    pros = (await client.get(queue, params={"kind": "pro"}, headers=headers)).json()
    [row] = [r for r in pros if r["professional_id"] == str(member.id)]
    assert (row["kind"], row["id"], row["sp_business_name"]) == (
        "pro",
        str(member_user.id),
        "Zayn Co",
    )
    assert str(self_pro.id) not in {r["professional_id"] for r in pros}

    both = (await client.get(queue, params={"kind": "all"}, headers=headers)).json()
    assert {(r["kind"], r["id"]) for r in both} >= {
        ("sp", str(sp.id)),
        ("pro", str(member_user.id)),
    }

    rejected = (
        await client.get(queue, params={"kind": "pro", "status": "rejected"}, headers=headers)
    ).json()
    assert str(member.id) not in {r["professional_id"] for r in rejected}, "?status still filters"


@pytest.mark.asyncio
async def test_admin_reviews_a_pro_by_professional_id(client, db_session, auth_headers):
    _, sp, self_pro, member, member_user = await _team(db_session, business_name="Zayn Co")
    member.kyc_status = KYCStatus.SUBMITTED
    profile = await ProviderService(db_session).get_profile(sp.id)
    profile.kyc_status = KYCStatus.SUBMITTED
    await db_session.flush()
    headers = _as(auth_headers, await _admin(db_session))
    base = f"/api/v1/admin/pros/{member.id}/kyc"

    review = await client.get(base, headers=headers)
    assert review.status_code == 200, review.text
    assert review.json()["kind"] == "pro"
    assert review.json()["professional_id"] == str(member.id)
    assert review.json()["sp_business_name"] == "Zayn Co"
    assert review.json()["user_id"] == str(member_user.id)

    resp = await client.post(f"{base}/verify", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["kyc_status"] == "verified"
    await db_session.refresh(member)
    await db_session.refresh(profile)
    assert member.kyc_status is KYCStatus.VERIFIED
    assert profile.kyc_status is KYCStatus.SUBMITTED, "never touches the SP row"

    # A Pro-for-self is reviewed as its SP, never here; unknown ids are 404 too.
    for pro_id in (self_pro.id, uuid.uuid4()):
        assert (
            await client.get(f"/api/v1/admin/pros/{pro_id}/kyc", headers=headers)
        ).status_code == 404


@pytest.mark.asyncio
async def test_admin_rejects_a_pro_and_non_admins_are_refused(client, db_session, auth_headers):
    _, _, _, member, member_user = await _team(db_session)
    member.kyc_status = KYCStatus.SUBMITTED
    await db_session.flush()
    url = f"/api/v1/admin/pros/{member.id}/kyc/reject"

    resp = await client.post(url, json={"reason": "blurry"}, headers=_as(auth_headers, member_user))
    assert resp.status_code == 403

    resp = await client.post(
        url, json={"reason": "blurry"}, headers=_as(auth_headers, await _admin(db_session))
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(member)
    assert (member.kyc_status, member.kyc_rejection_reason) == (KYCStatus.REJECTED, "blurry")


@pytest.mark.asyncio
async def test_admin_pro_review_shows_both_aadhaar_sides(client, db_session, auth_headers):
    _, _, _, member, member_user = await _team(db_session)
    with patch(UPLOAD):
        resp = await client.post(
            f"{ME}/kyc", files=_kyc_files(), headers=_as(auth_headers, member_user)
        )
    assert resp.status_code == 201, resp.text
    await db_session.refresh(member)
    with patch("app.services.provider_service.storage.get_presigned_url") as presign:
        presign.side_effect = lambda key: f"https://signed.example/{key}"
        body = (
            await client.get(
                f"/api/v1/admin/pros/{member.id}/kyc",
                headers=_as(auth_headers, await _admin(db_session)),
            )
        ).json()
    docs = member.kyc_docs
    assert body["aadhaar_front_url"] == f"https://signed.example/{docs['aadhaar_front_key']}"
    assert body["aadhaar_back_url"] == f"https://signed.example/{docs['aadhaar_back_key']}"
    assert body["aadhaar_url"] == body["aadhaar_front_url"]
