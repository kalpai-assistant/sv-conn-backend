"""SVC-464: soft-removed Pro auth — cross-matrix across ``/pros/me/*``.

Failure mode this guards:

    "A Pro with ``is_active=false`` retains access to a still-live booking
    because the filter only checked ``professional_id``."

A Pro row can be deactivated in three ways (SVC-320 PR 1 wording: "soft
remove"):
* an SP removes a team Pro — ``PATCH /providers/me/team/{pro_id}`` sets
  ``is_active = false``;
* the whole SP account is deleted (SVC-367) — every Pro of that SP goes
  inactive with ``display_name = "Deleted professional"``;
* a Pro's own user is deleted (SVC-367) — same mechanic, viewed from the
  user side.

In every case the ``professionals`` row stays in place with
``is_active = false`` because ~20 non-cascading FKs point at it (booking
history, ledger, reviews). The access check for a running booking must
re-read ``is_active`` on every call: a filter on ``professional_id``
alone — or a cache that was warmed before the deactivation — would let
the soft-removed person keep running the job.

This file runs the deactivated Pro through every single ``/pros/me/*``
endpoint plus ``GET /bookings/{id}``, for a matrix of:

* three ``booking.status`` values the Pro surface cares about: ACCEPTED,
  EN_ROUTE, IN_PROGRESS — the window where ``is_active`` actually
  matters (terminal states don't matter: the booking is done);
* two ``users.role`` values a Pro can hold in production: ``consumer``
  (the common path — invitees sign up as consumers) and ``provider``
  (the uncommon path where a Pro happens to also be a provider on
  another account);
* two ``is_active`` values: True (the baseline "still a Pro" row) and
  False (the ticket's failure mode).

Baseline rows should 200/204; deactivated rows should 404
(``NOT_A_PRO``) from any ``/pros/me`` endpoint, and 403 from
``GET /bookings/{id}``. A single assertion comment reminds future
readers of the named failure mode so a weakening of the check surfaces
loudly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.booking import BookingStatus
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus
from app.services.booking_service import BookingService
from app.services.provider_service import ProviderService
from app.tasks import notification_tasks
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service

ME = "/api/v1/pros/me"
JOBS = f"{ME}/jobs"


@pytest.fixture
def pushes():
    """Swallow push enqueues — none of these tests assert on them."""
    sent: list[str] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append(kw["event"]),
    ):
        yield sent


def _as(auth_headers, user):
    return auth_headers(str(user.id), user.role.value)


async def _team_with_member(db, member_role: str):
    """A verified SP + one member Pro whose own user has the given role."""
    service = await _service(db)
    sp = await _eligible_provider(db, service)
    await ProviderService(db).ensure_self_professional(sp.id)
    member_user = await UserFactory.create(db, role=member_role, name="Rohan")
    member = Professional(
        service_provider_id=sp.id,
        user_id=member_user.id,
        display_name="Rohan",
        is_online=True,
        kyc_status=KYCStatus.VERIFIED,
    )
    db.add(member)
    await db.flush()
    return service, sp, member, member_user


async def _assigned_booking(db, service, sp, member, *, target_status: BookingStatus):
    """A booking assigned to ``member`` and moved to ``target_status``.

    Status moves use the service layer rather than the routes so we don't
    perturb the pushes fixture.
    """
    _, booking = await _booking(db, service=service)
    booking, _, _ = await BookingService(db).assign(booking.id, sp.id, member.id)
    if target_status is BookingStatus.EN_ROUTE:
        await BookingService(db).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.EN_ROUTE,
            changed_by=member.user_id,
        )
    elif target_status is BookingStatus.IN_PROGRESS:
        await BookingService(db).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.EN_ROUTE,
            changed_by=member.user_id,
        )
        await BookingService(db).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.IN_PROGRESS,
            changed_by=member.user_id,
            otp=booking.doorstep_otp,
        )
    return booking


# ---------------------------------------------------------------------------
# The matrix: for every live status × every plausible role × is_active, hit
# every /pros/me/* endpoint and GET /bookings/{id}.
#
# is_active=True rows pass through (baseline sanity). is_active=False rows
# must be refused — named failure mode.
# ---------------------------------------------------------------------------

_LIVE_STATUSES = [
    BookingStatus.ACCEPTED,
    BookingStatus.EN_ROUTE,
    BookingStatus.IN_PROGRESS,
]
_ROLES = ["consumer", "provider"]


@pytest.mark.parametrize("target_status", _LIVE_STATUSES)
@pytest.mark.parametrize("role", _ROLES)
async def test_soft_removed_pro_loses_pros_me_surface(
    client, db_session, auth_headers, pushes, role, target_status
):
    """``GET /pros/me`` — a deactivated Pro must 404 ``NOT_A_PRO`` no matter
    which role their user row carries and no matter which live-booking
    status they were on.

    Named failure mode: a filter on ``professional_id`` alone (or a cached
    Pro object loaded before deactivation) could let them keep reading the
    surface. ``get_active_pro`` must re-read ``is_active`` every call.
    """
    service, sp, member, member_user = await _team_with_member(db_session, role)
    await _assigned_booking(db_session, service, sp, member, target_status=target_status)
    headers = _as(auth_headers, member_user)

    # Baseline: active Pro reads their profile fine.
    assert (await client.get(ME, headers=headers)).status_code == 200

    # The failure mode: soft-remove while a live booking is assigned.
    member.is_active = False
    await db_session.flush()

    resp = await client.get(ME, headers=headers)
    assert resp.status_code == 404, (
        f"SVC-464: soft-removed Pro (role={role}, status={target_status.value}) "
        f"still reached /pros/me. The filter only checked professional_id — "
        f"it must also require is_active=True. Got: {resp.status_code} {resp.text}"
    )
    assert resp.json()["code"] == "NOT_A_PRO"


@pytest.mark.parametrize("target_status", _LIVE_STATUSES)
@pytest.mark.parametrize("role", _ROLES)
async def test_soft_removed_pro_loses_pros_me_jobs(
    client, db_session, auth_headers, pushes, role, target_status
):
    """``GET /pros/me/jobs?scope=active`` — same answer: the assigned-job
    list must refuse a deactivated Pro, even for the booking they were
    still assigned to a second ago.
    """
    service, sp, member, member_user = await _team_with_member(db_session, role)
    await _assigned_booking(db_session, service, sp, member, target_status=target_status)
    headers = _as(auth_headers, member_user)

    assert (await client.get(JOBS, headers=headers)).status_code == 200

    member.is_active = False
    await db_session.flush()

    resp = await client.get(JOBS, headers=headers)
    assert resp.status_code == 404, (
        f"SVC-464: soft-removed Pro (role={role}, status={target_status.value}) "
        f"still saw /pros/me/jobs. {resp.status_code} {resp.text}"
    )


@pytest.mark.parametrize("target_status", _LIVE_STATUSES)
@pytest.mark.parametrize("role", _ROLES)
async def test_soft_removed_pro_cannot_move_a_live_booking(
    client, db_session, auth_headers, pushes, role, target_status
):
    """``POST /pros/me/jobs/{id}/en-route`` (and ``/start``, ``/complete``) —
    this is the ticket's exact failure mode: a Pro whose row is now inactive
    still moves a job that was assigned to them before the deactivation.

    No baseline call on the same session (would trip PRO_BUSY on the re-
    assignment). Baseline sanity for the active case is covered in
    test_pro_surface.py.
    """
    service, sp, member, member_user = await _team_with_member(db_session, role)
    booking = await _assigned_booking(db_session, service, sp, member, target_status=target_status)
    headers = _as(auth_headers, member_user)

    # ACCEPTED → /en-route; EN_ROUTE → /start; IN_PROGRESS → /complete.
    if target_status is BookingStatus.ACCEPTED:
        path = f"{JOBS}/{booking.id}/en-route"
        body: dict = {}
    elif target_status is BookingStatus.EN_ROUTE:
        path = f"{JOBS}/{booking.id}/start"
        body = {"otp": booking.doorstep_otp}
    else:
        path = f"{JOBS}/{booking.id}/complete"
        body = {}

    member.is_active = False
    await db_session.flush()

    resp = await client.post(path, json=body, headers=headers)
    assert resp.status_code == 404, (
        f"SVC-464: soft-removed Pro (role={role}) moved a live booking "
        f"({target_status.value}) they used to own. {resp.status_code} "
        f"{resp.text}. The access filter must not stop at "
        f"booking.professional_id — it must also require Professional.is_active."
    )


@pytest.mark.parametrize("target_status", _LIVE_STATUSES)
@pytest.mark.parametrize("role", _ROLES)
async def test_soft_removed_pro_loses_get_booking_access(
    client, db_session, auth_headers, pushes, role, target_status
):
    """``GET /bookings/{id}`` admits the assigned Pro (SVC-320 PR 3) — and
    must stop admitting them the moment ``is_active`` flips.
    """
    service, sp, member, member_user = await _team_with_member(db_session, role)
    booking = await _assigned_booking(db_session, service, sp, member, target_status=target_status)
    headers = _as(auth_headers, member_user)

    assert (await client.get(f"/api/v1/bookings/{booking.id}", headers=headers)).status_code == 200

    member.is_active = False
    await db_session.flush()

    resp = await client.get(f"/api/v1/bookings/{booking.id}", headers=headers)
    assert resp.status_code == 403, (
        f"SVC-464: soft-removed Pro (role={role}, status={target_status.value}) "
        f"still read GET /bookings/{{id}}. "
        f"Got {resp.status_code} {resp.text}. is_assigned_pro in "
        f"app/services/booking_service.py must also check Professional.is_active."
    )


async def test_soft_removed_pro_loses_online_toggle(client, db_session, auth_headers, pushes):
    """``POST /pros/me/online`` — the heartbeat and online toggle must also
    refuse a deactivated Pro. A still-running client with a cached Pro id
    must not be able to flip their row's online flag back up.
    """
    service, sp, member, member_user = await _team_with_member(db_session, "consumer")
    headers = _as(auth_headers, member_user)
    # Baseline.
    assert (
        await client.post(f"{ME}/online", json={"is_online": False}, headers=headers)
    ).status_code == 200

    member.is_active = False
    await db_session.flush()

    resp = await client.post(f"{ME}/online", json={"is_online": True}, headers=headers)
    assert resp.status_code == 404, (
        f"SVC-464: soft-removed Pro still flipped /pros/me/online. "
        f"{resp.status_code} {resp.text}"
    )


async def test_soft_removed_pro_cannot_decline_a_live_job(client, db_session, auth_headers, pushes):
    """``POST /pros/me/jobs/{id}/decline`` — a decline from a deactivated Pro
    must 404, not succeed and reopen the job. Otherwise an ex-Pro could
    reopen bookings they no longer have any claim on.
    """
    service, sp, member, member_user = await _team_with_member(db_session, "consumer")
    booking = await _assigned_booking(
        db_session, service, sp, member, target_status=BookingStatus.ACCEPTED
    )
    headers = _as(auth_headers, member_user)

    member.is_active = False
    await db_session.flush()

    resp = await client.post(f"{JOBS}/{booking.id}/decline", headers=headers)
    assert resp.status_code == 404, (
        f"SVC-464: soft-removed Pro declined a live booking — "
        f"reopening it on a team they're no longer part of. "
        f"{resp.status_code} {resp.text}"
    )
