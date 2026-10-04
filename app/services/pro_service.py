"""
The Pro's own surface (SVC-320 PR 3): profile, jobs, online toggle, location
heartbeat, Aadhaar KYC.

A Pro is whoever has an *active* ``professionals`` row. No role is involved:
Pros who joined through an invite are consumer-role accounts. A solo SP's
Pro-for-self is the same person as the SP and follows the SP: its online toggle
is the SP's and its KYC is the SP's (Tushar, 2026-10-04).
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException
from app.models.booking import Booking, BookingStatus
from app.models.professional import Professional
from app.models.provider_profile import ProviderProfile
from app.services.booking_service import BookingService
from app.services.provider_service import ProviderService
from app.utils.geo import build_point_wkt

NOT_A_PRO = "NOT_A_PRO"
SELF_PRO_KYC_IS_SP_KYC = "SELF_PRO_KYC_IS_SP_KYC"

# A job is "active" while someone still has to do something on it.
ACTIVE_JOB = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)

JobScope = Literal["active", "past"]


class ProService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_active_pro(self, user_id: uuid.UUID) -> Professional:
        """The caller's active Pro row; 404 ``NOT_A_PRO`` without one. A user has
        at most one active row (PR 1's one-team-at-a-time rule)."""
        pro = await self.db.scalar(
            select(Professional).where(
                Professional.user_id == user_id, Professional.is_active.is_(True)
            )
        )
        if pro is None:
            raise AppException(
                "You aren't part of a team.",
                status_code=status.HTTP_404_NOT_FOUND,
                code=NOT_A_PRO,
            )
        return pro

    async def profile(self, pro: Professional) -> dict:
        """``GET /pros/me``: the Pro row, its SP's brand, and the KYC that
        applies to this person (the SP's, for a Pro-for-self)."""
        sp = await self.db.scalar(
            select(ProviderProfile).where(ProviderProfile.user_id == pro.service_provider_id)
        )
        kyc_status = sp.kyc_status if pro.is_self and sp is not None else pro.kyc_status
        return {
            "id": pro.id,
            "user_id": pro.user_id,
            "display_name": pro.display_name,
            "profile_photo_url": pro.profile_photo_url,
            "is_self": pro.is_self,
            "is_online": pro.is_online,
            "kyc_status": kyc_status,
            "kyc_rejection_reason": (
                sp.kyc_rejection_reason if pro.is_self and sp else pro.kyc_rejection_reason
            ),
            "last_location_at": pro.last_location_at,
            "service_provider": {
                "user_id": pro.service_provider_id,
                "business_name": sp.business_name if sp else None,
                "business_image_url": sp.business_image_url if sp else None,
            },
        }

    async def list_jobs(
        self, pro: Professional, scope: JobScope, page: int, per_page: int
    ) -> tuple[list[Booking], int]:
        """Bookings assigned to this Pro, newest first. ``active`` = accepted,
        en route or in progress; ``past`` = everything else."""
        in_scope = (
            Booking.status.in_(ACTIVE_JOB) if scope == "active" else ~Booking.status.in_(ACTIVE_JOB)
        )
        where = (Booking.professional_id == pro.id, in_scope)
        total = await self.db.scalar(select(func.count()).select_from(Booking).where(*where))
        rows = (
            await self.db.scalars(
                select(Booking)
                .where(*where)
                # id as tie-break so pages never repeat or skip a row.
                .order_by(Booking.created_at.desc(), Booking.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        ).all()
        bookings = list(rows)
        service = BookingService(self.db)
        await service.attach_addresses(bookings)
        await service.attach_items(bookings)
        await service.attach_status_logs(bookings)
        return bookings, total or 0

    async def set_online(self, pro: Professional, is_online: bool) -> Professional:
        """A team Pro's own switch. A Pro-for-self goes through the SP toggle,
        which mirrors onto this row and enforces its service-area rule."""
        if pro.is_self:
            await ProviderService(self.db).set_online_status(pro.user_id, is_online)
            await self.db.refresh(pro)
            return pro
        pro.is_online = is_online
        await self.db.flush()
        return pro

    async def set_location(self, pro: Professional, lat: float, lng: float) -> Professional:
        """The ~30 s heartbeat. Stored only: dispatch doesn't read it yet, and it
        doesn't feed live tracking (that's the tracking WebSockets, SVC-335)."""
        pro.last_location = build_point_wkt(lat, lng)
        pro.last_location_at = datetime.now(UTC)
        await self.db.flush()
        return pro

    async def job_transition(
        self,
        pro: Professional,
        booking_id: uuid.UUID,
        new_status: BookingStatus,
        otp: str | None = None,
    ) -> Booking:
        """En route / start / complete, by the Pro the job is assigned to.

        The same ``transition_status`` the provider routes use, so the same
        state machine and doorstep-OTP check; the route then runs the same
        side effects (``notify_transition``). Not this Pro's job → 404, the
        same answer as decline gives.
        """
        booking = await self.db.get(Booking, booking_id)
        if booking is None or booking.professional_id != pro.id:
            raise AppException(
                "Job not found.", status_code=status.HTTP_404_NOT_FOUND, code="JOB_NOT_FOUND"
            )
        return await BookingService(self.db).transition_status(
            booking_id=booking_id,
            new_status=new_status,
            changed_by=pro.user_id,
            otp=otp,
        )

    async def submit_kyc(self, pro: Professional, **images) -> Professional:
        """Aadhaar identity KYC for a team Pro, onto the Pro row only (SVC-327).

        Same upload + AI verifier path as the SP's, through
        ``ProviderService.store_kyc_submission``. A Pro-for-self's KYC is the
        SP's, so it is refused here with a pointer to ``/providers/me/kyc``.
        """
        if pro.is_self:
            raise AppException(
                "Your identity KYC is your business KYC. Submit it from your provider profile.",
                status_code=status.HTTP_409_CONFLICT,
                code=SELF_PRO_KYC_IS_SP_KYC,
            )
        await ProviderService(self.db).store_kyc_submission(
            pro, key_prefix=f"kyc/pros/{pro.id}", **images
        )
        return pro
