"""
Provider service: profile management, KYC field encryption, online-status
toggling, and geospatial discovery.
"""
import uuid
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
)
from app.utils.geo import build_point_wkt, st_dwithin_filter, distance_expression
from app.utils.encryption import encrypt


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
