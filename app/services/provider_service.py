"""
Provider service: profile management, KYC field encryption, online-status
toggling, and geospatial discovery.
"""
import asyncio
import io
import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_profile import ProviderProfile, KYCStatus
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.exceptions import (
    ServiceNotFound,
    UnauthorizedAction,
    DuplicateSubscription,
    SubscriptionNotFound,
    UserNotFound,
    AppException,
)
from app.utils.geo import build_point_wkt, st_dwithin_filter, distance_expression
from app.utils.encryption import encrypt
from app.utils import storage


class ProviderService:
    """Manages provider profile data and service area queries."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Profile CRUD
    # ------------------------------------------------------------------

    async def get_profile(self, user_id: uuid.UUID) -> ProviderProfile | None:
        result = await self.db.execute(
            select(ProviderProfile).where(ProviderProfile.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create_profile(self, user_id: uuid.UUID) -> tuple[ProviderProfile, bool]:
        profile = await self.get_profile(user_id)
        is_new = profile is None
        if is_new:
            profile = ProviderProfile(user_id=user_id)
            self.db.add(profile)
            await self.db.flush()
        return profile, is_new

    async def update_profile(
        self,
        user_id: uuid.UUID,
        aadhaar: str | None,
        pan: str | None,
        upi_id: str | None,
        bank_account: str | None,
        service_area_lat: float | None,
        service_area_lng: float | None,
        service_area_radius_km: int | None,
        languages: list[str] | None = None,
    ) -> ProviderProfile:
        profile, _ = await self.get_or_create_profile(user_id)

        # Encrypt PII before persisting
        if aadhaar is not None:
            profile.aadhaar_encrypted = encrypt(aadhaar)
        if pan is not None:
            profile.pan_encrypted = encrypt(pan)
        if upi_id is not None:
            profile.upi_id_encrypted = encrypt(upi_id)
        if bank_account is not None:
            profile.bank_account_encrypted = encrypt(bank_account)

        if service_area_lat is not None and service_area_lng is not None:
            profile.service_area_center = build_point_wkt(service_area_lat, service_area_lng)
        if service_area_radius_km is not None:
            profile.service_area_radius_km = service_area_radius_km

        # Already validated + de-duplicated by the schema; an empty list clears it.
        if languages is not None:
            profile.languages = languages

        await self.db.flush()
        return profile

    async def update_photo(
        self,
        user_id: uuid.UUID,
        photo_bytes: bytes,
        content_type: str,
        ext: str,
    ) -> ProviderProfile:
        """
        Store/replace the provider's profile photo in S3 and persist its URL.

        Re-uploading writes a fresh UUID key, so the old object is orphaned and
        left to the bucket lifecycle policy — consistent with KYC (see submit_kyc).
        """
        profile, _ = await self.get_or_create_profile(user_id)

        # Unguessable key; bucket is never public but this blocks enumeration.
        key = f"provider-photos/{user_id}/{uuid.uuid4().hex}.{ext}"

        # boto3 is sync — offload to a thread so the event loop stays responsive.
        try:
            url = await asyncio.to_thread(
                storage.upload_file,
                io.BytesIO(photo_bytes),
                key,
                content_type=content_type,
            )
        except Exception as exc:
            raise AppException(
                f"Profile photo upload failed: {exc}",
                status_code=502,
            ) from exc

        profile.profile_photo_url = url
        await self.db.flush()
        return profile

    async def set_online_status(self, user_id: uuid.UUID, is_online: bool) -> ProviderProfile:
        profile, _ = await self.get_or_create_profile(user_id)
        profile.is_online = is_online
        await self.db.flush()
        return profile

    # ------------------------------------------------------------------
    # Geo Discovery
    # ------------------------------------------------------------------

    async def find_nearby(
        self,
        lat: float,
        lng: float,
        radius_km: float = 10.0,
        category_id: uuid.UUID | None = None,
    ) -> list[dict]:
        """
        Return online providers whose service_area_center is within radius_km
        of the given coordinates, ordered by distance ascending.
        """
        dist_expr = distance_expression(ProviderProfile.service_area_center, lat, lng)

        stmt = (
            select(ProviderProfile, dist_expr.label("distance_km"))
            .where(ProviderProfile.is_online.is_(True))
            .where(st_dwithin_filter(ProviderProfile.service_area_center, lat, lng, radius_km))
            .order_by(dist_expr)
        )

        result = await self.db.execute(stmt)
        rows = result.all()

        return [
            {**row.ProviderProfile.__dict__, "distance_km": row.distance_km}
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Service Subscriptions
    # ------------------------------------------------------------------

    async def subscribe_to_service(
        self, provider_user_id: uuid.UUID, service_id: uuid.UUID
    ) -> ProviderServiceModel:
        profile = await self.get_profile(provider_user_id)
        if profile is None:
            raise UnauthorizedAction("Provider profile not found")
        if profile.kyc_status != KYCStatus.VERIFIED:
            raise UnauthorizedAction(
                "Only verified providers can subscribe to services"
            )

        service = await self.db.get(Service, service_id)
        if service is None or not service.is_active:
            raise ServiceNotFound(service_id)

        existing = await self.db.execute(
            select(ProviderServiceModel).where(
                ProviderServiceModel.provider_id == provider_user_id,
                ProviderServiceModel.service_id == service_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise DuplicateSubscription(service_id)

        subscription = ProviderServiceModel(
            provider_id=provider_user_id,
            service_id=service_id,
        )
        self.db.add(subscription)
        await self.db.flush()
        return subscription

    async def unsubscribe_from_service(
        self, provider_user_id: uuid.UUID, service_id: uuid.UUID
    ) -> None:
        result = await self.db.execute(
            select(ProviderServiceModel).where(
                ProviderServiceModel.provider_id == provider_user_id,
                ProviderServiceModel.service_id == service_id,
            )
        )
        sub = result.scalar_one_or_none()
        if sub is None:
            raise SubscriptionNotFound(service_id)

        await self.db.delete(sub)
        await self.db.flush()

    async def list_subscriptions(
        self, provider_user_id: uuid.UUID
    ) -> list[ProviderServiceModel]:
        """Return the provider's current service subscriptions."""
        result = await self.db.execute(
            select(ProviderServiceModel).where(
                ProviderServiceModel.provider_id == provider_user_id
            )
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Rating update (called by review_service after new review)
    # ------------------------------------------------------------------

    async def refresh_avg_rating(
        self, provider_user_id: uuid.UUID, new_rating: int
    ) -> None:
        """Incrementally update avg_rating using a running weighted average."""
        profile = await self.get_profile(provider_user_id)
        if not profile:
            return
        n = profile.total_completed_jobs or 0
        if n == 0:
            profile.avg_rating = float(new_rating)
        else:
            profile.avg_rating = round(
                (profile.avg_rating * n + new_rating) / (n + 1), 2
            )
        await self.db.flush()

    # ------------------------------------------------------------------
    # KYC submission + admin review (SC-31)
    # ------------------------------------------------------------------

    async def submit_kyc(
        self,
        user_id: uuid.UUID,
        aadhaar_bytes: bytes,
        aadhaar_content_type: str,
        aadhaar_ext: str,
        selfie_bytes: bytes,
        selfie_content_type: str,
        selfie_ext: str,
    ) -> ProviderProfile:
        """
        Upload KYC documents to S3 and mark the profile as SUBMITTED.

        Re-submission is allowed while status is PENDING / REJECTED; the
        previous S3 keys are overwritten (UUIDs differ so old files remain in
        the bucket — a lifecycle policy should expire them, see backend/INFRA.md).

        SUBMITTED and VERIFIED states refuse re-upload — admin must reject
        first if the docs need to change.
        """
        profile, _ = await self.get_or_create_profile(user_id)

        if profile.kyc_status in (KYCStatus.SUBMITTED, KYCStatus.VERIFIED):
            raise UnauthorizedAction(
                "Cannot re-submit KYC while it is pending review or verified. "
                "Contact support to reset."
            )

        # Unguessable S3 keys; we never expose the bucket publicly anyway, but
        # this prevents enumeration if a presigned URL leaks.
        aadhaar_key = f"kyc/{user_id}/aadhaar-{uuid.uuid4().hex}.{aadhaar_ext}"
        selfie_key = f"kyc/{user_id}/selfie-{uuid.uuid4().hex}.{selfie_ext}"

        # boto3 is sync — offload to a thread so the event loop stays responsive.
        try:
            await asyncio.to_thread(
                storage.upload_file,
                io.BytesIO(aadhaar_bytes),
                aadhaar_key,
                content_type=aadhaar_content_type,
            )
            await asyncio.to_thread(
                storage.upload_file,
                io.BytesIO(selfie_bytes),
                selfie_key,
                content_type=selfie_content_type,
            )
        except Exception as exc:
            raise AppException(
                f"KYC document upload failed: {exc}",
                status_code=502,
            ) from exc

        profile.kyc_docs = {
            "aadhaar_key": aadhaar_key,
            "selfie_key": selfie_key,
            "aadhaar_content_type": aadhaar_content_type,
            "selfie_content_type": selfie_content_type,
        }
        profile.kyc_status = KYCStatus.SUBMITTED
        profile.kyc_submitted_at = datetime.now(timezone.utc)
        # Clear any prior rejection notes so the admin UI doesn't surface stale ones.
        profile.kyc_reviewed_at = None
        profile.kyc_rejection_reason = None

        await self.db.flush()
        return profile

    async def verify_kyc(self, user_id: uuid.UUID) -> ProviderProfile:
        """Admin-only: approve a provider's submitted KYC."""
        profile = await self.get_profile(user_id)
        if profile is None:
            raise UserNotFound(user_id)
        if profile.kyc_status != KYCStatus.SUBMITTED:
            raise UnauthorizedAction(
                f"Can only verify SUBMITTED KYC (current: {profile.kyc_status.value})"
            )

        profile.kyc_status = KYCStatus.VERIFIED
        profile.kyc_reviewed_at = datetime.now(timezone.utc)
        profile.kyc_rejection_reason = None
        await self.db.flush()
        return profile

    async def reject_kyc(self, user_id: uuid.UUID, reason: str) -> ProviderProfile:
        """Admin-only: reject a provider's submitted KYC with a reason."""
        profile = await self.get_profile(user_id)
        if profile is None:
            raise UserNotFound(user_id)
        if profile.kyc_status != KYCStatus.SUBMITTED:
            raise UnauthorizedAction(
                f"Can only reject SUBMITTED KYC (current: {profile.kyc_status.value})"
            )

        profile.kyc_status = KYCStatus.REJECTED
        profile.kyc_reviewed_at = datetime.now(timezone.utc)
        profile.kyc_rejection_reason = reason
        await self.db.flush()
        return profile
