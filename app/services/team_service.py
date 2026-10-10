"""
SP team management (SVC-320, Phase B PR 1): invites, the team list, soft
removal, and accept-invite.

The SP is still the ``provider_profiles`` row (keyed by ``user_id``); a team is
that SP's active ``professionals`` rows, its Pro-for-self included. See
``docs/pro-entity/LLD.md``, "SP team management".
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from fastapi import status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException
from app.models.booking import ACTIVE_JOB_STATUSES, Booking
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_team_invite import (
    INVITE_CODE_DIGITS,
    InviteStatus,
    ProviderTeamInvite,
)
from app.models.user import User
from app.services.provider_matching import assignable_team_pro_exists
from app.services.provider_service import ProviderService, professional_display_name
from app.utils import sms_client

logger = logging.getLogger(__name__)


def new_invite_code() -> str:
    """A zero-padded random code, e.g. ``"004217"``."""
    return f"{secrets.randbelow(10**INVITE_CODE_DIGITS):0{INVITE_CODE_DIGITS}d}"


INVITE_TTL = timedelta(days=7)
# Fresh codes to try when one collides with another live invite to the same
# phone. Each collision is a ~1-in-10^6 event, so this is never exhausted in
# practice; it only keeps the loop bounded.
_CODE_ATTEMPTS = 5

# Stable codes the frontend can branch on (SVC-321 / SVC-322).
INVITER_PAYOUT_KYC_INCOMPLETE = "INVITER_PAYOUT_KYC_INCOMPLETE"
CANNOT_INVITE_SELF = "CANNOT_INVITE_SELF"
ALREADY_ON_TEAM = "ALREADY_ON_TEAM"
INVITE_ALREADY_PENDING = "INVITE_ALREADY_PENDING"
INVITE_NOT_FOUND = "INVITE_NOT_FOUND"
INVITE_NOT_PENDING = "INVITE_NOT_PENDING"
INVITE_NO_LONGER_VALID = "INVITE_NO_LONGER_VALID"
INVITE_CODE_UNAVAILABLE = "INVITE_CODE_UNAVAILABLE"
ALREADY_PRO_ELSEWHERE = "ALREADY_PRO_ELSEWHERE"
PRO_NOT_FOUND = "PRO_NOT_FOUND"
CANNOT_REMOVE_SELF = "CANNOT_REMOVE_SELF"
PRO_HAS_ACTIVE_JOB = "PRO_HAS_ACTIVE_JOB"
USER_HAS_ACTIVE_JOBS = "USER_HAS_ACTIVE_JOBS"
NO_TEAM_TO_DISPATCH = "NO_TEAM_TO_DISPATCH"


class TeamService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # SP side
    # ------------------------------------------------------------------

    async def create_invite(self, sp_user: User, invitee_phone: str) -> ProviderTeamInvite:
        """Create an invite and text it to ``invitee_phone`` (already canonical).

        The KYC gate (SVC-327) runs first: a Pro's payouts settle to the SP, so
        an SP without verified payout KYC must not grow a team. Rejected before
        any token exists or SMS is sent.

        The SMS goes out after the row is flushed but before the request
        commits, so an MSG91 failure (502) rolls the invite back and the SP can
        simply retry. The reverse, SMS sent but commit failing, texts a code
        that doesn't exist; the invitee gets a 404 on accept. Rare and harmless.
        """
        # A plain read: a provider with no profile yet is simply unverified, and
        # a rejected invite shouldn't create an SP as a side effect.
        profile = await ProviderService(self.db).get_profile(sp_user.id)
        if profile is None or profile.kyc_status != KYCStatus.VERIFIED:
            raise AppException(
                "Complete your KYC before inviting team members. "
                "Your team's earnings are paid to you, so payouts must be set up first.",
                status_code=status.HTTP_403_FORBIDDEN,
                code=INVITER_PAYOUT_KYC_INCOMPLETE,
            )

        if invitee_phone == sp_user.phone:
            raise AppException(
                "You can't invite yourself.",
                status_code=status.HTTP_400_BAD_REQUEST,
                code=CANNOT_INVITE_SELF,
            )

        already_member = await self.db.scalar(
            select(Professional.id)
            .join(User, User.id == Professional.user_id)
            .where(
                Professional.service_provider_id == sp_user.id,
                Professional.is_active.is_(True),
                User.phone == invitee_phone,
            )
        )
        if already_member is not None:
            raise AppException(
                "This person is already on your team.",
                status_code=status.HTTP_409_CONFLICT,
                code=ALREADY_ON_TEAM,
            )

        now = datetime.now(UTC)
        pending = await self.db.scalar(
            select(ProviderTeamInvite)
            .where(
                ProviderTeamInvite.invited_by == sp_user.id,
                ProviderTeamInvite.invitee_phone == invitee_phone,
                ProviderTeamInvite.status == InviteStatus.PENDING.value,
            )
            .with_for_update()
        )
        if pending is not None:
            if pending.is_live(now):
                raise AppException(
                    "An invite to this number is already pending. Revoke it to send a new one.",
                    status_code=status.HTTP_409_CONFLICT,
                    code=INVITE_ALREADY_PENDING,
                )
            # Lazy expiry: free the partial unique index for the new invite.
            pending.status = InviteStatus.EXPIRED.value
            await self.db.flush()

        invite = await self._insert_with_fresh_code(sp_user.id, invitee_phone, now)

        await sms_client.send_team_invite(
            invitee_phone,
            # var1: the brand first, then the owner's name. Same blank handling.
            business_name=professional_display_name(profile.business_name, sp_user.name),
            code=invite.token,
        )
        logger.info("team.invite_created invite_id=%s sp=%s", invite.id, sp_user.id)
        return invite

    async def _insert_with_fresh_code(
        self, sp_id: uuid.UUID, invitee_phone: str, now: datetime
    ) -> ProviderTeamInvite:
        """Insert a PENDING invite with a new code, retrying on a code collision.

        Each attempt runs in a savepoint, since two partial unique indexes can
        reject it. The (SP, phone) one means a concurrent identical invite won
        the race: the same 409 as the check above. The (phone, code) one means
        this phone already holds a live invite (from another SP) with this code,
        so draw another.
        """
        for _ in range(_CODE_ATTEMPTS):
            invite = ProviderTeamInvite(
                invited_by=sp_id,
                invitee_phone=invitee_phone,
                token=new_invite_code(),
                status=InviteStatus.PENDING.value,
                expires_at=now + INVITE_TTL,
            )
            try:
                async with self.db.begin_nested():
                    self.db.add(invite)
                    await self.db.flush()
            except IntegrityError:
                duplicate = await self.db.scalar(
                    select(ProviderTeamInvite.id).where(
                        ProviderTeamInvite.invited_by == sp_id,
                        ProviderTeamInvite.invitee_phone == invitee_phone,
                        ProviderTeamInvite.status == InviteStatus.PENDING.value,
                    )
                )
                if duplicate is not None:
                    raise AppException(
                        "An invite to this number is already pending. "
                        "Revoke it to send a new one.",
                        status_code=status.HTTP_409_CONFLICT,
                        code=INVITE_ALREADY_PENDING,
                    ) from None
                logger.info("team.invite_code_collision sp=%s", sp_id)
                continue
            await self.db.refresh(invite)
            return invite
        raise AppException(
            "Couldn't generate an invite code. Please try again.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=INVITE_CODE_UNAVAILABLE,
        )

    async def revoke_invite(self, sp_id: uuid.UUID, invite_id: uuid.UUID) -> ProviderTeamInvite:
        invite = await self.db.scalar(
            select(ProviderTeamInvite)
            .where(ProviderTeamInvite.id == invite_id, ProviderTeamInvite.invited_by == sp_id)
            .with_for_update()
        )
        # Someone else's invite is indistinguishable from a missing one.
        if invite is None:
            raise AppException(
                "Invite not found.", status_code=status.HTTP_404_NOT_FOUND, code=INVITE_NOT_FOUND
            )
        now = datetime.now(UTC)
        if not invite.is_live(now):
            raise AppException(
                f"This invite can't be revoked; it is {self._effective_status(invite, now)}.",
                status_code=status.HTTP_409_CONFLICT,
                code=INVITE_NOT_PENDING,
            )
        invite.status = InviteStatus.REVOKED.value
        invite.revoked_at = now
        await self.db.flush()
        await self.db.refresh(invite)
        return invite

    async def list_invites(self, sp_id: uuid.UUID) -> list[ProviderTeamInvite]:
        """Every invite this SP has sent, newest first, any status (SVC-320 PR 2).

        Lets the SP UI find a pending invite to revoke after a reload, and shows
        what became of the rest. Stale PENDING rows are expired on the way out,
        and the flip is saved, so the list never shows a dead invite as live.
        """
        invites = list(
            (
                await self.db.execute(
                    select(ProviderTeamInvite)
                    .where(ProviderTeamInvite.invited_by == sp_id)
                    .order_by(ProviderTeamInvite.created_at.desc(), ProviderTeamInvite.id)
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        stale = [
            i for i in invites if i.status == InviteStatus.PENDING.value and not i.is_live(now)
        ]
        for invite in stale:
            invite.status = InviteStatus.EXPIRED.value
        if stale:
            await self.db.flush()
        return invites

    async def list_team(self, sp_id: uuid.UUID) -> list[Professional]:
        """Active Pros under this SP, Pro-for-self first, then by join date."""
        # Creates the SP (and so its Pro-for-self) if this is a provider's first
        # call, like every other /providers/me read.
        await ProviderService(self.db).get_or_create_profile(sp_id)
        result = await self.db.execute(
            select(Professional)
            .where(
                Professional.service_provider_id == sp_id,
                Professional.is_active.is_(True),
            )
            .order_by((Professional.user_id != sp_id), Professional.created_at, Professional.id)
        )
        return list(result.scalars().all())

    async def remove_pro(self, sp_id: uuid.UUID, pro_id: uuid.UUID) -> bool:
        """Soft-remove a Pro: ``is_active = false``. The row stays, since
        bookings and reviews reference it. Returns whether the SP's own
        Pro-for-self was switched back on (SVC-326).

        An SP can't remove its own Pro-for-self; going "off tools" is
        ``set_accepts_direct_jobs`` instead.

        This is the only place a Pro is deactivated, so it is where the SVC-326
        invariant is kept: an SP that has switched off taking jobs itself must
        have another active Pro. Removing the last one switches the Pro-for-self
        back on in the same transaction, under the same row lock the switch
        takes, so a concurrent switch-off can't slip between the two.

        A Pro holding a job in ``ACTIVE_JOB_STATUSES`` is refused (409
        ``PRO_HAS_ACTIVE_JOB``, SVC-329): removal ends their access to the job,
        and past ACCEPTED nobody could re-pick it. Lock order: this Pro FOR
        UPDATE, then an unlocked read of bookings, then the Pro-for-self.
        ``BookingService.assign`` locks the booking and then this Pro FOR SHARE,
        so the two serialize on the Pro row; don't lock a booking in here.
        """
        pro = await self.db.scalar(
            select(Professional)
            .where(
                Professional.id == pro_id,
                Professional.service_provider_id == sp_id,
                Professional.is_active.is_(True),
            )
            .with_for_update()
        )
        if pro is None:
            raise AppException(
                "Team member not found.",
                status_code=status.HTTP_404_NOT_FOUND,
                code=PRO_NOT_FOUND,
            )
        if pro.is_self:
            raise AppException(
                "You can't remove yourself from your own team.",
                status_code=status.HTTP_400_BAD_REQUEST,
                code=CANNOT_REMOVE_SELF,
            )
        active_job = await self.db.scalar(
            select(Booking.id)
            .where(Booking.professional_id == pro.id, Booking.status.in_(ACTIVE_JOB_STATUSES))
            .limit(1)
        )
        if active_job is not None:
            raise AppException(
                f"{pro.display_name} is on a job. Reassign or finish it before removing them.",
                status_code=status.HTTP_409_CONFLICT,
                code=PRO_HAS_ACTIVE_JOB,
            )
        pro.is_active = False
        # A removed Pro must not look available to anything reading is_online.
        pro.is_online = False
        await self.db.flush()

        self_reenabled = False
        self_pro = await self._lock_self_pro(sp_id)
        if (
            self_pro is not None
            and not self_pro.accepts_direct_jobs
            and not await self._has_other_active_pro(sp_id)
        ):
            self_pro.accepts_direct_jobs = True
            await self.db.flush()
            self_reenabled = True
        logger.info(
            "team.pro_removed pro_id=%s sp=%s self_reenabled=%s", pro.id, sp_id, self_reenabled
        )
        return self_reenabled

    async def deactivate_user(
        self, user_id: uuid.UUID
    ) -> tuple[User, list[tuple[Professional, bool]]] | None:
        """Admin deactivation (SVC-329): ``users.is_active = false`` plus what
        keeps it from stranding work. Returns the user and each team-Pro row it
        removed with ``remove_pro``'s ``self_reenabled``, or ``None`` if unknown.

        Refused first (409 ``USER_HAS_ACTIVE_JOBS``, ids in
        ``details.booking_ids``) while the person holds a job: as the SP on a
        booking, or as its Pro. Nothing is written then; the admin resolves
        those (force-cancel, or the owner re-picks) and retries.

        Otherwise the person goes offline everywhere and each team-Pro row they
        hold is soft-removed through ``remove_pro``, so the other SP's SVC-326
        invariant holds. Their own Pro-for-self and their own team are left
        alone: eligibility already skips an inactive owner.

        The user row is locked FOR UPDATE first, the lock ``accept_invite``
        takes, so an accept can't add an active Pro row mid-way.
        """
        user = await self.db.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if user is None:
            return None

        rows = list(
            (await self.db.execute(select(Professional).where(Professional.user_id == user_id)))
            .scalars()
            .all()
        )
        active_pro_ids = [row.id for row in rows if row.is_active]
        held = Booking.provider_id == user_id
        if active_pro_ids:
            held = held | Booking.professional_id.in_(active_pro_ids)
        job_ids = list(
            (
                await self.db.execute(
                    select(Booking.id)
                    .where(held, Booking.status.in_(ACTIVE_JOB_STATUSES))
                    .order_by(Booking.created_at, Booking.id)
                )
            )
            .scalars()
            .all()
        )
        if job_ids:
            raise AppException(
                "This user is on active jobs. Resolve them before deactivating.",
                status_code=status.HTTP_409_CONFLICT,
                code=USER_HAS_ACTIVE_JOBS,
                details={"booking_ids": [str(i) for i in job_ids]},
            )

        user.is_active = False
        profile = await self.db.scalar(
            select(ProviderProfile).where(ProviderProfile.user_id == user_id)
        )
        if profile is not None:
            profile.is_online = False
        for row in rows:
            row.is_online = False
        await self.db.flush()
        removed = []
        for row in rows:
            if row.is_active and not row.is_self:
                removed.append((row, await self.remove_pro(row.service_provider_id, row.id)))
        logger.info("team.user_deactivated user=%s", user_id)
        return user, removed

    # ------------------------------------------------------------------
    # SP off tools (SVC-326)
    # ------------------------------------------------------------------

    async def get_self_pro(self, sp_id: uuid.UUID) -> tuple[Professional, bool]:
        """The SP's Pro-for-self and whether any team Pro is assignable now."""
        await ProviderService(self.db).get_or_create_profile(sp_id)
        self_pro = await ProviderService(self.db).ensure_self_professional(sp_id)
        return self_pro, await self._team_has_assignable_pro(sp_id)

    async def set_accepts_direct_jobs(
        self, sp_id: uuid.UUID, accepts: bool
    ) -> tuple[Professional, bool]:
        """Switch the SP's own job-taking on or off. Only new dispatch changes:
        bookings already on the Pro-for-self are left alone.

        Off needs another active Pro (409 ``NO_TEAM_TO_DISPATCH``), or the SP
        would stop being broadcast to with nobody to send. Whether that Pro is
        online or free right now doesn't matter here; while none is,
        eligibility simply leaves the SP out, and ``team_has_assignable_pro``
        tells the app to warn.
        """
        await ProviderService(self.db).get_or_create_profile(sp_id)
        await ProviderService(self.db).ensure_self_professional(sp_id)
        self_pro = await self._lock_self_pro(sp_id)
        if not accepts and not await self._has_other_active_pro(sp_id):
            raise AppException(
                "Add someone to your team before you stop taking jobs yourself.",
                status_code=status.HTTP_409_CONFLICT,
                code=NO_TEAM_TO_DISPATCH,
            )
        if self_pro.accepts_direct_jobs != accepts:
            self_pro.accepts_direct_jobs = accepts
            await self.db.flush()
            logger.info("team.self_accepts_direct_jobs sp=%s value=%s", sp_id, accepts)
        return self_pro, await self._team_has_assignable_pro(sp_id)

    async def _lock_self_pro(self, sp_id: uuid.UUID) -> Professional | None:
        # populate_existing: the row may already be in the identity map from an
        # earlier unlocked read; refresh it with what the lock now guards.
        return await self.db.scalar(
            select(Professional)
            .where(Professional.service_provider_id == sp_id, Professional.user_id == sp_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def _has_other_active_pro(self, sp_id: uuid.UUID) -> bool:
        return (
            await self.db.scalar(
                select(Professional.id)
                .where(
                    Professional.service_provider_id == sp_id,
                    Professional.user_id != sp_id,
                    Professional.is_active.is_(True),
                )
                .limit(1)
            )
            is not None
        )

    async def _team_has_assignable_pro(self, sp_id: uuid.UUID) -> bool:
        # The broadcast's own predicate, narrowed to this SP.
        return (
            await self.db.scalar(
                select(ProviderProfile.user_id).where(
                    ProviderProfile.user_id == sp_id, assignable_team_pro_exists()
                )
            )
            is not None
        )

    # ------------------------------------------------------------------
    # Invitee side
    # ------------------------------------------------------------------

    async def accept_invite(self, user: User, code: str) -> Professional:
        """Join the inviting SP's team as a Pro, with the code from the SMS.

        An invite is found by the code *and* the caller's own phone, so a code
        only works for the person it was texted to. A wrong code and someone
        else's valid code are the same 404: nothing reveals whether a code
        exists for another phone. The router limits this to 10 calls an hour
        per user, which is what makes a 6-digit code safe against guessing.

        One SP at a time (chain depth 1): a user with an *active* Pro row under
        another SP is refused. A Pro removed from SP A is free to join SP B.
        Rejoining an SP that removed you reactivates the old row, since
        ``UNIQUE (user_id, service_provider_id)`` allows only one.
        """
        invite = await self.db.scalar(
            select(ProviderTeamInvite)
            .where(
                ProviderTeamInvite.token == code,
                ProviderTeamInvite.invitee_phone == user.phone,
                ProviderTeamInvite.status == InviteStatus.PENDING.value,
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        if invite is None or not invite.is_live(now):
            await self._raise_for_unusable_code(user, code, invite)

        # Serialise this user's accepts, so two invites from different SPs
        # accepted at once can't both pass the one-SP check below.
        await self.db.execute(select(User.id).where(User.id == user.id).with_for_update())
        rows = (
            await self.db.execute(select(Professional).where(Professional.user_id == user.id))
        ).scalars()
        existing_here: Professional | None = None
        for row in rows:
            if row.service_provider_id == invite.invited_by:
                existing_here = row
            elif row.is_active:
                raise AppException(
                    "You're already on another team, and you can only be on one at a time.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code=ALREADY_PRO_ELSEWHERE,
                )

        if existing_here is not None and existing_here.is_active:
            raise AppException(
                "You're already on this team.",
                status_code=status.HTTP_409_CONFLICT,
                code=ALREADY_ON_TEAM,
            )
        if existing_here is not None:
            existing_here.is_active = True
            pro = existing_here
        else:
            pro = Professional(
                service_provider_id=invite.invited_by,
                user_id=user.id,
                display_name=professional_display_name(user.name),
            )
            self.db.add(pro)

        invite.status = InviteStatus.ACCEPTED.value
        invite.accepted_at = now
        invite.accepted_by_user_id = user.id
        await self.db.flush()
        await self.db.refresh(pro)
        logger.info(
            "team.invite_accepted invite_id=%s sp=%s pro_id=%s",
            invite.id,
            invite.invited_by,
            pro.id,
        )
        return pro

    async def _raise_for_unusable_code(
        self, user: User, code: str, pending: ProviderTeamInvite | None
    ) -> NoReturn:
        """410 when the code is one of the caller's own invites and is over;
        404 otherwise.

        Only the caller's own invites are consulted, so the 410 reveals nothing
        about anyone else's. It spares someone re-opening an old SMS a confusing
        "not found". Codes repeat over time, so the newest match wins.
        """
        invite = pending
        if invite is None:
            invite = await self.db.scalar(
                select(ProviderTeamInvite)
                .where(
                    ProviderTeamInvite.token == code,
                    ProviderTeamInvite.invitee_phone == user.phone,
                )
                .order_by(ProviderTeamInvite.created_at.desc())
                .limit(1)
            )
        if invite is None:
            raise AppException(
                "That invite code isn't valid. Check the code in your SMS.",
                status_code=status.HTTP_404_NOT_FOUND,
                code=INVITE_NOT_FOUND,
            )
        # 410, like a closed booking offer: the invite was real, it's over.
        raise AppException(
            f"This invite is {self._effective_status(invite, datetime.now(UTC))}. "
            "Ask for a new one if you still want to join.",
            status_code=status.HTTP_410_GONE,
            code=INVITE_NO_LONGER_VALID,
        )

    @staticmethod
    def _effective_status(invite: ProviderTeamInvite, now: datetime) -> str:
        if invite.status == InviteStatus.PENDING.value and invite.expires_at <= now:
            return "expired"
        return invite.status.lower()
