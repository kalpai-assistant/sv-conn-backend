"""
Admin team management (SVC-329): find an owner, see their team (removed Pros
included) and invite history, force-remove or reassign a Pro, and the audit
log every admin team mutation writes.

Mutations go through ``TeamService.remove_pro``, the only place a Pro is
deactivated, so the SVC-326 invariant and the active-job refusal hold for
admin actions too. See ``docs/pro-entity/LLD.md``, "Admin team management".
"""

import logging
import re
import uuid

from fastapi import status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException
from app.models.admin_action import AdminAction
from app.models.booking import ACTIVE_JOB_STATUSES, Booking
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_team_invite import ProviderTeamInvite
from app.models.user import User
from app.schemas.admin_team import (
    AdminActionRow,
    AdminProDetail,
    AdminProOwner,
    AdminProviderRow,
    AdminTeamPro,
)
from app.services.provider_service import professional_display_name
from app.services.team_service import ALREADY_ON_TEAM, PRO_NOT_FOUND, TeamService

logger = logging.getLogger(__name__)

SP_NOT_FOUND = "SP_NOT_FOUND"
CANNOT_REASSIGN_SELF = "CANNOT_REASSIGN_SELF"
SAME_SERVICE_PROVIDER = "SAME_SERVICE_PROVIDER"
TARGET_SP_NOT_FOUND = "TARGET_SP_NOT_FOUND"
TARGET_SP_INACTIVE = "TARGET_SP_INACTIVE"
TARGET_SP_KYC_INCOMPLETE = "TARGET_SP_KYC_INCOMPLETE"
USER_INACTIVE = "USER_INACTIVE"

_PHONE_NOISE_RE = re.compile(r"[\s\-()]")


def _escape_like(term: str) -> str:
    """Make ``%``, ``_`` and ``\\`` match themselves in an ILIKE pattern."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pro_not_found() -> AppException:
    return AppException(
        "Professional not found.", status_code=status.HTTP_404_NOT_FOUND, code=PRO_NOT_FOUND
    )


async def record_admin_action(
    db: AsyncSession,
    *,
    admin_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    reason: str | None,
    details: dict,
    sp_user_id: uuid.UUID | None = None,
    to_sp_user_id: uuid.UUID | None = None,
) -> AdminAction:
    """Append one ``admin_actions`` row: the only writer. Call it in the same
    transaction as the change it describes."""
    row = AdminAction(
        admin_user_id=admin_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        sp_user_id=sp_user_id,
        to_sp_user_id=to_sp_user_id,
        reason=reason,
        details=details,
    )
    db.add(row)
    await db.flush()
    return row


class AdminTeamService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def search_providers(
        self, q: str | None, page: int, per_page: int
    ) -> list[AdminProviderRow]:
        """Owners, newest first, optionally matching ``q`` against phone, name
        or business name. One statement per page, counts included.

        Phones are stored as ``+91XXXXXXXXXX`` and matched as a substring with
        spaces, dashes and brackets dropped, so ``98765``, ``9876543210`` and
        ``+91 98765 43210`` all find ``+919876543210``.
        """
        team_size = (
            select(
                Professional.service_provider_id.label("sp_id"),
                func.count().label("n"),
            )
            .where(
                Professional.is_active.is_(True),
                Professional.user_id != Professional.service_provider_id,
            )
            .group_by(Professional.service_provider_id)
            .subquery()
        )
        stmt = (
            select(User, ProviderProfile, func.coalesce(team_size.c.n, 0))
            .join(ProviderProfile, ProviderProfile.user_id == User.id)
            .outerjoin(team_size, team_size.c.sp_id == User.id)
        )
        term = (q or "").strip()
        if term:
            text_pattern = f"%{_escape_like(term)}%"
            phone_pattern = f"%{_escape_like(_PHONE_NOISE_RE.sub('', term))}%"
            stmt = stmt.where(
                or_(
                    User.phone.ilike(phone_pattern, escape="\\"),
                    User.name.ilike(text_pattern, escape="\\"),
                    ProviderProfile.business_name.ilike(text_pattern, escape="\\"),
                )
            )
        stmt = (
            stmt.order_by(User.created_at.desc(), User.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        return [
            AdminProviderRow(
                user_id=user.id,
                name=user.name,
                phone=user.phone,
                business_name=profile.business_name,
                kyc_status=profile.kyc_status,
                is_online=profile.is_online,
                user_is_active=user.is_active,
                active_pro_count=count,
                created_at=user.created_at,
            )
            for user, profile, count in (await self.db.execute(stmt)).all()
        ]

    async def _require_owner(self, sp_user_id: uuid.UUID) -> ProviderProfile:
        # A plain read: unlike /providers/me, admin must never create an SP.
        profile = await self.db.scalar(
            select(ProviderProfile).where(ProviderProfile.user_id == sp_user_id)
        )
        if profile is None:
            raise AppException(
                "Service provider not found.",
                status_code=status.HTTP_404_NOT_FOUND,
                code=SP_NOT_FOUND,
            )
        return profile

    def _team_rows_stmt(self) -> Select:
        """Pro rows with their person's phone and active flag and their active
        job count: one statement, whatever the team size."""
        jobs = (
            select(Booking.professional_id.label("pro_id"), func.count().label("n"))
            .where(
                Booking.professional_id.is_not(None),
                Booking.status.in_(ACTIVE_JOB_STATUSES),
            )
            .group_by(Booking.professional_id)
            .subquery()
        )
        return (
            select(Professional, User.phone, User.is_active, func.coalesce(jobs.c.n, 0))
            .join(User, User.id == Professional.user_id)
            .outerjoin(jobs, jobs.c.pro_id == Professional.id)
        )

    @staticmethod
    def _team_pro(pro: Professional, phone: str, user_active: bool, jobs: int) -> AdminTeamPro:
        return AdminTeamPro(
            id=pro.id,
            service_provider_id=pro.service_provider_id,
            user_id=pro.user_id,
            display_name=pro.display_name,
            phone=phone,
            user_is_active=user_active,
            profile_photo_url=pro.profile_photo_url,
            kyc_status=pro.kyc_status,
            is_online=pro.is_online,
            is_active=pro.is_active,
            is_self=pro.is_self,
            accepts_direct_jobs=pro.accepts_direct_jobs,
            active_job_count=jobs,
            created_at=pro.created_at,
        )

    async def team(self, sp_user_id: uuid.UUID) -> list[AdminTeamPro]:
        """Every Pro row under this owner, removed ones included: Pro-for-self
        first, then active before removed, then by join date."""
        await self._require_owner(sp_user_id)
        stmt = (
            self._team_rows_stmt()
            .where(Professional.service_provider_id == sp_user_id)
            .order_by(
                Professional.user_id != sp_user_id,
                Professional.is_active.desc(),
                Professional.created_at,
                Professional.id,
            )
        )
        return [self._team_pro(*row) for row in (await self.db.execute(stmt)).all()]

    async def _team_pro_by_id(self, pro_id: uuid.UUID) -> AdminTeamPro | None:
        row = (
            await self.db.execute(self._team_rows_stmt().where(Professional.id == pro_id))
        ).first()
        return self._team_pro(*row) if row is not None else None

    async def invites(self, sp_user_id: uuid.UUID) -> list[ProviderTeamInvite]:
        """The owner's invite history, every status, via ``list_invites`` (which
        saves its stale PENDING -> EXPIRED flip; the router commits)."""
        await self._require_owner(sp_user_id)
        return await TeamService(self.db).list_invites(sp_user_id)

    async def pro_detail(self, pro_id: uuid.UUID) -> AdminProDetail:
        pro = await self._team_pro_by_id(pro_id)
        if pro is None:
            raise _pro_not_found()
        owner, profile = (
            await self.db.execute(
                select(User, ProviderProfile)
                .outerjoin(ProviderProfile, ProviderProfile.user_id == User.id)
                .where(User.id == pro.service_provider_id)
            )
        ).one()
        return AdminProDetail(
            **pro.model_dump(),
            service_provider=AdminProOwner(
                user_id=owner.id,
                name=owner.name,
                phone=owner.phone,
                business_name=profile.business_name if profile is not None else None,
            ),
        )

    async def actions(
        self,
        *,
        target_type: str | None,
        target_id: uuid.UUID | None,
        sp_user_id: uuid.UUID | None,
        page: int,
        per_page: int,
    ) -> list[AdminActionRow]:
        """Audit rows, newest first. ``sp_user_id`` matches an owner on either
        side of the action, so a reassign shows in both owners' histories."""
        stmt = select(AdminAction, User.name).join(User, User.id == AdminAction.admin_user_id)
        if target_type is not None:
            stmt = stmt.where(AdminAction.target_type == target_type)
        if target_id is not None:
            stmt = stmt.where(AdminAction.target_id == target_id)
        if sp_user_id is not None:
            stmt = stmt.where(
                or_(AdminAction.sp_user_id == sp_user_id, AdminAction.to_sp_user_id == sp_user_id)
            )
        stmt = (
            stmt.order_by(AdminAction.created_at.desc(), AdminAction.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        return [
            AdminActionRow(
                id=row.id,
                admin_user_id=row.admin_user_id,
                admin_name=admin_name,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                sp_user_id=row.sp_user_id,
                to_sp_user_id=row.to_sp_user_id,
                reason=row.reason,
                details=row.details,
                created_at=row.created_at,
            )
            for row, admin_name in (await self.db.execute(stmt)).all()
        ]

    # ------------------------------------------------------------------
    # Mutations: each writes exactly one admin_actions row
    # ------------------------------------------------------------------

    async def deactivate_user(
        self, admin_id: uuid.UUID, user_id: uuid.UUID, reason: str | None
    ) -> bool:
        """``TeamService.deactivate_user`` plus its audit row. False for an
        unknown user (nothing written).

        ``sp_user_id`` is the person themself if they own an SP; otherwise the
        owner whose team they were removed from, if any. (One person has at
        most one active Pro row, and an owner's is their Pro-for-self.)
        """
        result = await TeamService(self.db).deactivate_user(user_id)
        if result is None:
            return False
        _, removed = result
        is_owner = (
            await self.db.scalar(
                select(ProviderProfile.id).where(ProviderProfile.user_id == user_id)
            )
            is not None
        )
        sp_user_id = (
            user_id if is_owner else (removed[0][0].service_provider_id if removed else None)
        )
        await record_admin_action(
            self.db,
            admin_id=admin_id,
            action="user_deactivated",
            target_type="user",
            target_id=user_id,
            reason=reason,
            sp_user_id=sp_user_id,
            details={
                "removed_professional_ids": [str(pro.id) for pro, _ in removed],
                "self_reenabled_sp_ids": [
                    str(pro.service_provider_id) for pro, reenabled in removed if reenabled
                ],
            },
        )
        return True

    async def force_remove(self, admin_id: uuid.UUID, pro_id: uuid.UUID, reason: str) -> bool:
        """Soft-remove a Pro on the owner's behalf, through ``remove_pro``: so
        404 if missing or already removed, 400 for a Pro-for-self, 409 while on
        a job. Returns ``self_reenabled``."""
        pro = await self.db.get(Professional, pro_id)
        if pro is None:
            raise _pro_not_found()
        sp_id = pro.service_provider_id
        self_reenabled = await TeamService(self.db).remove_pro(sp_id, pro_id)
        await record_admin_action(
            self.db,
            admin_id=admin_id,
            action="pro_force_removed",
            target_type="professional",
            target_id=pro_id,
            reason=reason,
            sp_user_id=sp_id,
            details={
                "sp_user_id": str(sp_id),
                "user_id": str(pro.user_id),
                "self_reenabled": self_reenabled,
            },
        )
        logger.info("admin.pro_force_removed pro_id=%s sp=%s admin=%s", pro_id, sp_id, admin_id)
        return self_reenabled

    async def reassign(
        self,
        admin_id: uuid.UUID,
        pro_id: uuid.UUID,
        target_sp_user_id: uuid.UUID,
        reason: str,
    ) -> tuple[uuid.UUID, AdminTeamPro, bool, bool]:
        """Move a person from their owner's team to another owner's, by admin
        authority: no invite, no accept. Returns ``(old_professional_id, new
        row, reactivated, self_reenabled)``.

        KYC is never copied between owners (Option A): KYC lives on the row.
        If the target already has an old row for this person it is
        reactivated with its own KYC, as ``accept_invite`` does; otherwise a
        new row starts PENDING. Either way it starts offline.

        Lock order: the person's user row FOR UPDATE (``accept_invite``'s
        lock, so an accept can't add a second active row meanwhile), then the
        Pro row and the old owner's Pro-for-self inside ``remove_pro``. Two
        admins moving the same Pro serialize on the user row; the second finds
        the source row removed and gets a 404.
        """
        pro = await self.db.get(Professional, pro_id)
        if pro is None or not pro.is_active:
            raise _pro_not_found()
        if pro.is_self:
            raise AppException(
                "An owner's own professional profile can't be moved to another team.",
                status_code=status.HTTP_400_BAD_REQUEST,
                code=CANNOT_REASSIGN_SELF,
            )
        from_sp_id = pro.service_provider_id
        if target_sp_user_id == from_sp_id:
            raise AppException(
                "This professional is already on that team.",
                status_code=status.HTTP_400_BAD_REQUEST,
                code=SAME_SERVICE_PROVIDER,
            )

        target = (
            await self.db.execute(
                select(ProviderProfile, User.is_active)
                .join(User, User.id == ProviderProfile.user_id)
                .where(ProviderProfile.user_id == target_sp_user_id)
            )
        ).first()
        if target is None:
            raise AppException(
                "Target service provider not found.",
                status_code=status.HTTP_404_NOT_FOUND,
                code=TARGET_SP_NOT_FOUND,
            )
        target_profile, target_active = target
        if not target_active:
            raise AppException(
                "The target service provider's account is deactivated.",
                status_code=status.HTTP_409_CONFLICT,
                code=TARGET_SP_INACTIVE,
            )
        # The invite gate (SVC-327): the Pro's payouts settle to the new owner.
        if target_profile.kyc_status != KYCStatus.VERIFIED:
            raise AppException(
                "The target service provider hasn't completed KYC.",
                status_code=status.HTTP_409_CONFLICT,
                code=TARGET_SP_KYC_INCOMPLETE,
            )

        person = await self.db.scalar(
            select(User)
            .where(User.id == pro.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not person.is_active:
            raise AppException(
                "This person's account is deactivated.",
                status_code=status.HTTP_409_CONFLICT,
                code=USER_INACTIVE,
            )
        rows = (
            (
                await self.db.execute(
                    select(Professional)
                    .where(Professional.user_id == person.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        source = next((r for r in rows if r.id == pro_id), None)
        if source is None or not source.is_active or source.service_provider_id != from_sp_id:
            # Moved or removed by someone else while we waited for the lock.
            raise _pro_not_found()
        existing = next((r for r in rows if r.service_provider_id == target_sp_user_id), None)
        if existing is not None and existing.is_active:
            raise AppException(
                "This person is already on the target team.",
                status_code=status.HTTP_409_CONFLICT,
                code=ALREADY_ON_TEAM,
            )

        self_reenabled = await TeamService(self.db).remove_pro(from_sp_id, pro_id)

        reactivated = existing is not None
        if existing is not None:
            existing.is_active = True
            existing.is_online = False
            new_row = existing
        else:
            new_row = Professional(
                service_provider_id=target_sp_user_id,
                user_id=person.id,
                display_name=professional_display_name(person.name),
                is_online=False,
            )
            self.db.add(new_row)
        await self.db.flush()

        await record_admin_action(
            self.db,
            admin_id=admin_id,
            action="pro_reassigned",
            target_type="professional",
            target_id=pro_id,
            reason=reason,
            sp_user_id=from_sp_id,
            to_sp_user_id=target_sp_user_id,
            details={
                "user_id": str(person.id),
                "from_sp_user_id": str(from_sp_id),
                "to_sp_user_id": str(target_sp_user_id),
                "to_professional_id": str(new_row.id),
                "reactivated": reactivated,
                "kyc_status": new_row.kyc_status.value,
                "self_reenabled": self_reenabled,
            },
        )
        logger.info(
            "admin.pro_reassigned pro_id=%s from=%s to=%s new_pro_id=%s admin=%s",
            pro_id,
            from_sp_id,
            target_sp_user_id,
            new_row.id,
            admin_id,
        )
        return pro_id, await self._team_pro_by_id(new_row.id), reactivated, self_reenabled
