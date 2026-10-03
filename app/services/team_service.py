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
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus
from app.models.provider_team_invite import (
    INVITE_CODE_DIGITS,
    InviteStatus,
    ProviderTeamInvite,
)
from app.models.user import User
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

    async def remove_pro(self, sp_id: uuid.UUID, pro_id: uuid.UUID) -> None:
        """Soft-remove a Pro: ``is_active = false``. The row stays, since
        bookings and reviews reference it.

        An SP can't remove its own Pro-for-self here. Going "off tools" is
        SVC-326, which also changes dispatch so an SP whose team has no eligible
        Pro stops being broadcast to. Doing it here, before that exists, would
        leave a solo SP broadcast-eligible with nobody to do the job.
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
        pro.is_active = False
        # A removed Pro must not look available to anything reading is_online.
        pro.is_online = False
        await self.db.flush()
        logger.info("team.pro_removed pro_id=%s sp=%s", pro.id, sp_id)

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
