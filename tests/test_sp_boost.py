"""
SVC-414: the new-SP boost starts on the SP's own first KYC approval and counts
its completed jobs.

The two places an SP becomes VERIFIED both start it: the admin approval
(``verify_kyc``, also behind ``scripts/verify_kyc.py``) and the AI auto-approve
branch of ``_score_and_maybe_auto_approve``. ``boost_eligible_at`` takes the
approval's ``kyc_reviewed_at`` and the count resets to 0, once per SP: a later
approval leaves both alone. A team Pro's approval never touches its SP.

Every completion adds 1 to ``boost_completed_jobs_count`` once the boost has
started, for the SP's own jobs and its team Pros' (``booking.provider_id`` is
the SP either way), and leaves it at 0 when it hasn't. The ranking sheet's
``is_boosted`` decides when the window ends; the count isn't capped here.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.booking import BookingStatus
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.services.kyc_verifier import KYCExtracted, KYCLLMResponse, KYCVerifier
from app.services.provider_service import ProviderService
from tests.factories.user_factory import UserFactory
from tests.test_kyc_queue import _bot
from tests.test_kyc_verifier import _StubClient
from tests.test_pro_surface import JOBS, _as, _assigned_job, _team, pushes  # noqa: F401

KYC_VERIFY = "/api/v1/admin/providers/{}/kyc/verify"
PRO_KYC_VERIFY = "/api/v1/admin/pros/{}/kyc/verify"
BOOKING = "/api/v1/bookings/{}"
AUTO_APPROVE_FLAG = "app.services.kyc_verifier.get_auto_approve_enabled"
DOWNLOAD = "app.services.provider_service.storage.download_bytes"
EARLIER = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

_CLEAN = KYCLLMResponse(
    extracted=KYCExtracted(aadhaar_last4="1234", name_en="Ravi Kumar", gender="M"),
    selfie_matches_aadhaar=True,
    selfie_match_confidence=0.95,
    is_aadhaar=True,
    is_tampered=False,
)
_BLURRY = KYCLLMResponse(
    extracted=KYCExtracted(aadhaar_last4="1234", name_en="Ravi Kumar", gender="M"),
    selfie_matches_aadhaar=True,
    selfie_match_confidence=0.55,
    is_aadhaar=True,
    is_tampered=False,
    is_blurry_front=True,
)


async def _sp(db, kyc: KYCStatus = KYCStatus.SUBMITTED, **fields):
    user = await UserFactory.create(db, role="provider", name="Ravi Kumar")
    profile = ProviderProfile(user_id=user.id, kyc_status=kyc, **fields)
    db.add(profile)
    await db.flush()
    return user, profile


async def _admin_approve(client, auth_headers, db, user):
    admin = await UserFactory.create(db, role="admin")
    resp = await client.post(KYC_VERIFY.format(user.id), headers=_as(auth_headers, admin))
    assert resp.status_code == 200, resp.text


def _boost(profile: ProviderProfile) -> tuple:
    return (profile.boost_eligible_at, profile.boost_completed_jobs_count)


# --- Boost start ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_approval_starts_the_boost_at_the_review_time(client, auth_headers, db_session):
    user, profile = await _sp(db_session)
    assert _boost(profile) == (None, 0)

    await _admin_approve(client, auth_headers, db_session, user)

    await db_session.refresh(profile)
    assert profile.kyc_status is KYCStatus.VERIFIED
    assert profile.kyc_reviewed_at is not None
    assert _boost(profile) == (profile.kyc_reviewed_at, 0)


@pytest.mark.asyncio
async def test_a_second_approval_keeps_the_first_boost(client, auth_headers, db_session):
    user, profile = await _sp(db_session)
    await _admin_approve(client, auth_headers, db_session, user)
    await db_session.refresh(profile)
    first_start = profile.boost_eligible_at
    # Some jobs done since, then the SP is back in review (as after a rejection
    # and a resubmission, which is the only way a VERIFIED SP gets there).
    profile.boost_completed_jobs_count = 3
    profile.kyc_status = KYCStatus.SUBMITTED
    profile.kyc_reviewed_at = None
    await db_session.flush()

    await _admin_approve(client, auth_headers, db_session, user)

    await db_session.refresh(profile)
    assert profile.kyc_status is KYCStatus.VERIFIED
    assert profile.kyc_reviewed_at > first_start
    assert _boost(profile) == (first_start, 3)


@pytest.mark.asyncio
async def test_a_service_approval_of_an_sp_with_a_boost_leaves_it(db_session):
    """The same guard behind ``scripts/verify_kyc.py``, which calls the service."""
    _, profile = await _sp(db_session, boost_eligible_at=EARLIER, boost_completed_jobs_count=7)

    await ProviderService(db_session).verify_kyc(profile.user_id)

    assert profile.kyc_status is KYCStatus.VERIFIED
    assert _boost(profile) == (EARLIER, 7)


async def _score(db, profile, llm: KYCLLMResponse, *, flag: bool) -> None:
    await _bot(db)
    profile.kyc_docs = {
        "aadhaar_front_key": "kyc/front.png",
        "aadhaar_back_key": "kyc/back.png",
        "selfie_key": "kyc/selfie.png",
    }
    verifier = KYCVerifier(client=_StubClient(response=llm))
    with patch(DOWNLOAD, return_value=b"bytes"), patch(AUTO_APPROVE_FLAG, return_value=flag):
        await ProviderService(db)._score_and_maybe_auto_approve(profile, verifier=verifier)
    await db.flush()
    await db.refresh(profile)


@pytest.mark.asyncio
async def test_ai_auto_approval_starts_the_boost(db_session):
    _, profile = await _sp(db_session)

    await _score(db_session, profile, _CLEAN, flag=True)

    assert profile.kyc_ai_verdict["outcome"] == "auto_approve"
    assert profile.kyc_status is KYCStatus.VERIFIED
    assert _boost(profile) == (profile.kyc_reviewed_at, 0)


@pytest.mark.asyncio
async def test_ai_auto_approval_keeps_an_earlier_boost(db_session):
    _, profile = await _sp(db_session, boost_eligible_at=EARLIER, boost_completed_jobs_count=2)

    await _score(db_session, profile, _CLEAN, flag=True)

    assert profile.kyc_status is KYCStatus.VERIFIED
    assert _boost(profile) == (EARLIER, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm", "flag", "outcome"),
    [(_BLURRY, True, "needs_review"), (_CLEAN, False, "auto_approve")],
    ids=["needs_review", "auto_approve_flag_off"],
)
async def test_no_auto_approval_no_boost(db_session, llm, flag, outcome):
    _, profile = await _sp(db_session)

    await _score(db_session, profile, llm, flag=flag)

    assert profile.kyc_ai_verdict["outcome"] == outcome
    assert profile.kyc_status is KYCStatus.SUBMITTED
    assert _boost(profile) == (None, 0)


@pytest.mark.asyncio
async def test_a_team_pro_approval_leaves_its_sp_alone(client, auth_headers, db_session):
    _, sp, _, member, _ = await _team(db_session)
    profile = await ProviderService(db_session).get_profile(sp.id)
    member.kyc_status = KYCStatus.SUBMITTED
    await db_session.flush()
    await db_session.refresh(profile)
    before = (profile.kyc_status, profile.kyc_reviewed_at, *_boost(profile))
    assert _boost(profile) == (None, 0)
    admin = await UserFactory.create(db_session, role="admin")

    resp = await client.post(PRO_KYC_VERIFY.format(member.id), headers=_as(auth_headers, admin))

    assert resp.status_code == 200, resp.text
    await db_session.refresh(member)
    await db_session.refresh(profile)
    assert member.kyc_status is KYCStatus.VERIFIED
    assert (profile.kyc_status, profile.kyc_reviewed_at, *_boost(profile)) == before


# --- Job counter ------------------------------------------------------------------


async def _job_in_progress(db, *, boost_started: bool):
    service, sp, _, member, member_user = await _team(db)
    profile = await ProviderService(db).get_profile(sp.id)
    if boost_started:
        profile.boost_eligible_at = datetime.now(UTC) - timedelta(days=1)
        profile.boost_completed_jobs_count = 1
    profile.total_completed_jobs = 5
    _, booking = await _assigned_job(db, service, sp, member)
    booking.status = BookingStatus.IN_PROGRESS
    await db.flush()
    return sp, member_user, profile, booking


async def _complete(client, auth_headers, booking, sp, member_user, by: str):
    if by == "sp":
        resp = await client.patch(
            f"{BOOKING.format(booking.id)}/complete", headers=_as(auth_headers, sp)
        )
    else:
        resp = await client.post(
            f"{JOBS}/{booking.id}/complete", headers=_as(auth_headers, member_user)
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("pushes")
@pytest.mark.parametrize("by", ["sp", "team_pro"])
async def test_a_completed_job_counts_toward_a_started_boost(client, auth_headers, db_session, by):
    sp, member_user, profile, booking = await _job_in_progress(db_session, boost_started=True)
    started = profile.boost_eligible_at

    await _complete(client, auth_headers, booking, sp, member_user, by)

    await db_session.refresh(booking)
    await db_session.refresh(profile)
    assert booking.status is BookingStatus.COMPLETED
    assert profile.total_completed_jobs == 6
    assert _boost(profile) == (started, 2)


@pytest.mark.asyncio
@pytest.mark.usefixtures("pushes")
@pytest.mark.parametrize("by", ["sp", "team_pro"])
async def test_a_completed_job_without_a_boost_only_counts_the_total(
    client, auth_headers, db_session, by
):
    sp, member_user, profile, booking = await _job_in_progress(db_session, boost_started=False)

    await _complete(client, auth_headers, booking, sp, member_user, by)

    await db_session.refresh(booking)
    await db_session.refresh(profile)
    assert booking.status is BookingStatus.COMPLETED
    assert profile.total_completed_jobs == 6
    assert _boost(profile) == (None, 0)
