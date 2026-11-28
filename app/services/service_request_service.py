"""SVC-436: the quota and spam-ban rules for SP service requests.

Two helpers, both called by later stories, never by a route directly:

* ``can_sp_submit_request``: SA3 calls it before inserting a request. SA3 maps
  ``SP_REQUEST_BANNED`` to 423 and ``REQUEST_QUOTA_EXCEEDED`` to 429. The rate
  limiter also returns 429, so a client must tell them apart by ``code``.
* ``trigger_spam_check_on_rejection``: SA5-SA7 call it after writing a
  rejection. It runs on rejection, not on submission, so an SP is never banned
  mid-submit.

**Locking.** Both lock the SP's ``provider_profiles`` row (``FOR UPDATE``), so
two concurrent submits can't both see 4 PENDING and make 6, and two concurrent
rejections can't both insert a ban. The lock lasts only until the *caller's*
transaction ends. The caller must do its write (insert the request, or write the
rejection) in the same transaction as the helper, and commit after it.

The thresholds are constants, not config: they are a product decision to review,
not an ops knob.

"now" is Postgres ``now()``, read once per call. That is the transaction's start
time, so every comparison and the new ``banned_until`` use one clock.
"""

import uuid
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ProviderNotFound
from app.models.provider_profile import ProviderProfile
from app.models.service_request import REJECTED_STATUSES, ServiceRequest, ServiceRequestStatus
from app.models.sp_request_ban import SpRequestBan

MAX_PENDING_REQUESTS = 5
BAN_REJECTED_COUNT = 10
BAN_MIN_RESOLVED = 5
# A ban needs rejected / resolved > 7/10, compared in integers so exactly 70%
# can never round into a ban.
BAN_RATE_NUMERATOR = 7
BAN_RATE_DENOMINATOR = 10
BAN_DAYS = 30

SP_REQUEST_BANNED = "SP_REQUEST_BANNED"
REQUEST_QUOTA_EXCEEDED = "REQUEST_QUOTA_EXCEEDED"

_REJECTED = [s.value for s in REJECTED_STATUSES]


class SubmitCheck(NamedTuple):
    """``allowed, reason, banned_until = ...``. ``reason`` is None when allowed.
    ``banned_until`` is set only for ``SP_REQUEST_BANNED``, for the 423 body."""

    allowed: bool
    reason: str | None
    banned_until: datetime | None = None


class ServiceRequestService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def can_sp_submit_request(self, sp_id: uuid.UUID) -> SubmitCheck:
        """Can this SP submit a new request? An active ban comes first, then the
        pending quota.

        Locks the SP's ``provider_profiles`` row until the caller's transaction
        ends, so the caller must insert the request in that same transaction.
        Raises ``ProviderNotFound`` if ``sp_id`` is not an SP.
        """
        await self._lock_sp(sp_id)
        now = await self._now()

        banned_until = await self._active_ban_until(sp_id, now)
        if banned_until is not None:
            return SubmitCheck(False, SP_REQUEST_BANNED, banned_until)

        pending = await self.db.scalar(
            select(func.count())
            .select_from(ServiceRequest)
            .where(
                ServiceRequest.sp_id == sp_id,
                ServiceRequest.status == ServiceRequestStatus.PENDING.value,
            )
        )
        if pending >= MAX_PENDING_REQUESTS:
            return SubmitCheck(False, REQUEST_QUOTA_EXCEEDED)
        return SubmitCheck(True, None)

    async def trigger_spam_check_on_rejection(self, sp_id: uuid.UUID) -> SpRequestBan | None:
        """Re-check the SP's record after a rejection was written, and ban the SP
        if it crosses a threshold. Returns the ban row (new or extended), or None.

        Only requests resolved after the SP's most recent *ended* ban count, so a
        ban that ended (cleared, or expired) gives a fresh start. Within that
        window, ban if rejected >= 10, or if resolved >= 5 and
        rejected / resolved > 0.7. Resolved = APPROVED + both REJECTED_*.
        PENDING never counts.

        A new ban runs 30 days. If a ban is already active, its ``banned_until``
        moves to the later of the two, and no row is added.

        Locks the SP's ``provider_profiles`` row until the caller's transaction
        ends, so the caller must have written the rejection in that same
        transaction. Raises ``ProviderNotFound`` if ``sp_id`` is not an SP.
        """
        await self._lock_sp(sp_id)
        now = await self._now()

        window_start = await self._last_ban_end(sp_id, now)
        is_rejected = ServiceRequest.status.in_(_REJECTED)
        conditions = [
            ServiceRequest.sp_id == sp_id,
            ServiceRequest.status != ServiceRequestStatus.PENDING.value,
        ]
        if window_start is not None:
            conditions.append(ServiceRequest.resolved_at > window_start)
        resolved, rejected = (
            await self.db.execute(
                select(
                    func.count(),
                    func.count().filter(is_rejected),
                ).where(*conditions)
            )
        ).one()

        reason = _ban_reason(resolved, rejected)
        if reason is None:
            return None

        new_until = now + timedelta(days=BAN_DAYS)
        active = (
            await self.db.execute(
                select(SpRequestBan)
                .where(SpRequestBan.sp_id == sp_id, *_active(now))
                .order_by(SpRequestBan.banned_until.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if active is not None:
            active.banned_until = max(active.banned_until, new_until)
            await self.db.flush()
            return active

        ban = SpRequestBan(sp_id=sp_id, banned_until=new_until, reason=reason)
        self.db.add(ban)
        await self.db.flush()
        return ban

    async def _lock_sp(self, sp_id: uuid.UUID) -> None:
        locked = await self.db.scalar(
            select(ProviderProfile.id).where(ProviderProfile.user_id == sp_id).with_for_update()
        )
        if locked is None:
            raise ProviderNotFound(sp_id)

    async def _now(self) -> datetime:
        return await self.db.scalar(select(func.now()))

    async def _active_ban_until(self, sp_id: uuid.UUID, now: datetime) -> datetime | None:
        return await self.db.scalar(
            select(func.max(SpRequestBan.banned_until)).where(
                SpRequestBan.sp_id == sp_id, *_active(now)
            )
        )

    async def _last_ban_end(self, sp_id: uuid.UUID, now: datetime) -> datetime | None:
        """When the SP's most recent ended ban ended, or None if none has.

        A ban ends at whichever comes first: the admin's clear, or
        ``banned_until``. A clear made after the ban had already expired doesn't
        move the end later, so the rejections between the two still count.
        """
        ended_at = case(
            (
                SpRequestBan.cleared_at.is_not(None),
                func.least(SpRequestBan.cleared_at, SpRequestBan.banned_until),
            ),
            else_=SpRequestBan.banned_until,
        )
        return await self.db.scalar(
            select(func.max(ended_at)).where(
                SpRequestBan.sp_id == sp_id,
                or_(SpRequestBan.cleared_at.is_not(None), SpRequestBan.banned_until <= now),
            )
        )


def _active(now: datetime):
    return (and_(SpRequestBan.banned_until > now, SpRequestBan.cleared_at.is_(None)),)


def _ban_reason(resolved: int, rejected: int) -> str | None:
    if rejected >= BAN_REJECTED_COUNT:
        return f"Spam detection: {rejected} rejected requests"
    if (
        resolved >= BAN_MIN_RESOLVED
        and rejected * BAN_RATE_DENOMINATOR > resolved * BAN_RATE_NUMERATOR
    ):
        return f"Spam detection: {rejected} of {resolved} resolved requests rejected"
    return None
