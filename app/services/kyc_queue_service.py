"""
The admin KYC queue (SVC-298, SVC-327, SVC-424): one list, and one count that
can't disagree with it, since both read through ``_sp_rows`` / ``_pro_rows``.

A row is left out of every status if nobody can act on it: a deleted account
(deletion wipes its documents) or a removed Pro (a re-added Pro reactivates the
same row, so it comes back by itself). A deactivated user stays in.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import User
from app.schemas.provider import KYCQueueRow

KYCQueueKind = Literal["sp", "pro", "all"]

# status=all is everything someone has submitted. KYCStatus.PENDING means no
# documents were uploaded, so it isn't a review state.
_ALL_STATUSES = (KYCStatus.SUBMITTED, KYCStatus.VERIFIED, KYCStatus.REJECTED)

# The SP a team Pro belongs to, for its business name.
_OwnerProfile = aliased(ProviderProfile)


@dataclass(frozen=True)
class KYCQueueFilter:
    """Which rows a ``?status=`` means, and the order they come in."""

    statuses: tuple[KYCStatus, ...]
    sort_field: Literal["kyc_submitted_at", "kyc_reviewed_at"]
    newest_first: bool
    # Only for auto_approved: reviewed by this user after this moment.
    reviewed_by: UUID | None = None
    reviewed_after: datetime | None = None

    def where(self, model) -> list:
        clauses = [model.kyc_status.in_(self.statuses)]
        if self.reviewed_by is not None:
            clauses += [
                model.kyc_reviewed_by == self.reviewed_by,
                model.kyc_reviewed_at > self.reviewed_after,
            ]
        return clauses

    def order(self, model) -> list:
        """A missing time sorts last either way, the same as ``_merge``. The id
        breaks ties, so a row can't repeat or vanish between pages."""
        column = getattr(model, self.sort_field)
        column = column.desc() if self.newest_first else column.asc()
        return [column.nulls_last(), model.id]


def kyc_queue_filter(status: str) -> KYCQueueFilter:
    """The filter for a ``?status=`` value. Raises ValueError on an unknown one."""
    if status == "all":
        return KYCQueueFilter(_ALL_STATUSES, "kyc_submitted_at", newest_first=True)
    if status == "auto_approved":
        # SVC-307 spot-check tab: not a KYCStatus, since the row IS verified. The
        # filter is on who verified it.
        return KYCQueueFilter(
            (KYCStatus.VERIFIED,),
            "kyc_reviewed_at",
            newest_first=True,
            reviewed_by=UUID(get_settings().KYC_VERIFIER_BOT_USER_ID),
            reviewed_after=datetime.now(UTC) - timedelta(days=7),
        )
    kyc_status = KYCStatus(status)
    # Finding a past decision is the point of these two. Submitted drains FIFO.
    if kyc_status in (KYCStatus.REJECTED, KYCStatus.VERIFIED):
        return KYCQueueFilter((kyc_status,), "kyc_reviewed_at", newest_first=True)
    return KYCQueueFilter((kyc_status,), "kyc_submitted_at", newest_first=False)


def _sp_rows(*columns, queue_filter: KYCQueueFilter):
    return (
        select(*columns)
        .select_from(User)
        .join(ProviderProfile, ProviderProfile.user_id == User.id)
        .where(User.deleted_at.is_(None), *queue_filter.where(ProviderProfile))
    )


def _pro_rows(*columns, queue_filter: KYCQueueFilter):
    # Team Pros only: a Pro-for-self's KYC is its SP's, reviewed as the SP.
    return (
        select(*columns)
        .select_from(User)
        .join(Professional, Professional.user_id == User.id)
        .join(_OwnerProfile, _OwnerProfile.user_id == Professional.service_provider_id)
        .where(
            User.deleted_at.is_(None),
            Professional.is_active.is_(True),
            Professional.user_id != Professional.service_provider_id,
            *queue_filter.where(Professional),
        )
    )


def _sp_row(user: User, profile: ProviderProfile) -> KYCQueueRow:
    return KYCQueueRow(
        id=user.id,
        phone=user.phone,
        name=user.name,
        email=user.email,
        kyc_status=profile.kyc_status,
        kyc_submitted_at=profile.kyc_submitted_at,
        kyc_reviewed_at=profile.kyc_reviewed_at,
        kyc_rejection_reason=profile.kyc_rejection_reason,
    )


def _pro_row(user: User, pro: Professional, business_name: str | None) -> KYCQueueRow:
    return KYCQueueRow(
        id=user.id,
        phone=user.phone,
        name=user.name,
        email=user.email,
        kyc_status=pro.kyc_status,
        kyc_submitted_at=pro.kyc_submitted_at,
        kyc_reviewed_at=pro.kyc_reviewed_at,
        kyc_rejection_reason=pro.kyc_rejection_reason,
        kind="pro",
        professional_id=pro.id,
        sp_user_id=pro.service_provider_id,
        sp_business_name=business_name,
    )


def _merge(rows: list[KYCQueueRow], queue_filter: KYCQueueFilter) -> list[KYCQueueRow]:
    """Python's sort is stable, so equal times keep their order within a kind."""
    field = queue_filter.sort_field
    dated = [row for row in rows if getattr(row, field) is not None]
    undated = [row for row in rows if getattr(row, field) is None]
    dated.sort(key=lambda row: getattr(row, field), reverse=queue_filter.newest_first)
    return dated + undated


class KYCQueueService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _sp_stmt(self, queue_filter: KYCQueueFilter):
        return _sp_rows(User, ProviderProfile, queue_filter=queue_filter).order_by(
            *queue_filter.order(ProviderProfile)
        )

    def _pro_stmt(self, queue_filter: KYCQueueFilter):
        return _pro_rows(
            User, Professional, _OwnerProfile.business_name, queue_filter=queue_filter
        ).order_by(*queue_filter.order(Professional))

    async def list_rows(
        self, queue_filter: KYCQueueFilter, kind: KYCQueueKind, page: int, per_page: int
    ) -> list[KYCQueueRow]:
        offset = (page - 1) * per_page
        if kind == "sp":
            result = await self.db.execute(
                self._sp_stmt(queue_filter).offset(offset).limit(per_page)
            )
            return [_sp_row(*row) for row in result.all()]
        if kind == "pro":
            result = await self.db.execute(
                self._pro_stmt(queue_filter).offset(offset).limit(per_page)
            )
            return [_pro_row(*row) for row in result.all()]

        # all: the first page*per_page of each kind, merged in the same order, then
        # this page cut out of the merge. Admin-scale volumes, so two bounded reads.
        head = page * per_page
        sps = await self.db.execute(self._sp_stmt(queue_filter).limit(head))
        pros = await self.db.execute(self._pro_stmt(queue_filter).limit(head))
        rows = [_sp_row(*row) for row in sps.all()] + [_pro_row(*row) for row in pros.all()]
        return _merge(rows, queue_filter)[offset : offset + per_page]

    async def count(self, queue_filter: KYCQueueFilter, kind: KYCQueueKind) -> int:
        total = 0
        if kind in ("sp", "all"):
            total += await self.db.scalar(_sp_rows(func.count(), queue_filter=queue_filter))
        if kind in ("pro", "all"):
            total += await self.db.scalar(_pro_rows(func.count(), queue_filter=queue_filter))
        return total
