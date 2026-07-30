"""
Provider service: profile management, KYC field encryption, online-status
toggling, and geospatial discovery.
"""
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_profile import ProviderProfile, KYCStatus, PayoutMethod
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.exceptions import (
    ServiceNotFound,
    UnauthorizedAction,
    DuplicateSubscription,
    SubscriptionNotFound,
    InvalidPayoutMethod,
)
from app.utils.geo import build_point_wkt, st_dwithin_filter, distance_expression
from app.utils.encryption import encrypt, decrypt


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
        payout_vpa: str | None = None,
        payout_account_number: str | None = None,
        payout_ifsc: str | None = None,
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

        # Payout destination — encrypted at rest, never returned decrypted.
        #
        # Saving a method makes it the active one, and deliberately leaves the other
        # method's stored values alone (SVC-148): a provider who switches keeps the
        # old destination on file so they can flip back without re-entering it.
        # Removal is an explicit action, never a side effect of saving.
        # The schema rejects sending both methods at once, so at most one of these
        # branches makes itself active — no arbitrary tie-break.
        if payout_vpa is not None:
            profile.payout_vpa_encrypted = encrypt(payout_vpa)
            profile.payout_method = PayoutMethod.UPI
            profile.payout_display = payout_vpa
        if payout_account_number is not None:
            profile.payout_account_number_encrypted = encrypt(payout_account_number)
            profile.payout_method = PayoutMethod.BANK
            profile.payout_display = self._mask_account(payout_account_number)
        if payout_ifsc is not None:
            profile.payout_ifsc_encrypted = encrypt(payout_ifsc)

        if service_area_lat is not None and service_area_lng is not None:
            profile.service_area_center = build_point_wkt(service_area_lat, service_area_lng)
        if service_area_radius_km is not None:
            profile.service_area_radius_km = service_area_radius_km

        await self.db.flush()
        return profile

    @staticmethod
    def _mask_account(account_number: str) -> str:
        """Bank account as shown back to its owner: last 4 visible, rest masked.

        Last-4 is the convention everywhere a person is shown their own account —
        Indian net banking, GPay/PhonePe, the Razorpay and Stripe dashboards — and is
        enough for them to recognise which account they saved. A UPI VPA gets no
        masking: it is a public handle you hand out in order to be paid, so hiding it
        would cost usability and protect nothing.
        """
        return "•" * max(len(account_number) - 4, 0) + account_number[-4:]

    async def remove_payout_method(self, user_id: uuid.UUID) -> ProviderProfile:
        """Erase every stored payout destination. Explicit action only.

        Deliberately clears *both* methods, not just the active one: "remove my
        payout details" means the platform holds nothing, and leaving the inactive
        method behind would keep PII we have been asked to delete.
        """
        profile, _ = await self.get_or_create_profile(user_id)
        profile.payout_vpa_encrypted = None
        profile.payout_account_number_encrypted = None
        profile.payout_ifsc_encrypted = None
        profile.payout_method = None
        profile.payout_display = None
        await self.db.flush()
        return profile

    async def set_active_payout_method(
        self, user_id: uuid.UUID, method: PayoutMethod
    ) -> ProviderProfile:
        """Switch payouts to an already-stored method, without re-entering it.

        Rejects a method with nothing on file — activating a destination we cannot
        actually pay to would fail later at payout time, when the provider is no
        longer around to correct it.
        """
        profile, _ = await self.get_or_create_profile(user_id)

        if method is PayoutMethod.UPI and not profile.payout_vpa_encrypted:
            raise InvalidPayoutMethod("No UPI ID on file to switch to")
        if method is PayoutMethod.BANK and not (
            profile.payout_account_number_encrypted and profile.payout_ifsc_encrypted
        ):
            raise InvalidPayoutMethod("No bank account on file to switch to")

        profile.payout_method = method
        # The display label follows the active method, so it must be recomputed from
        # the stored value — the only point where payout PII is decrypted outside the
        # payout itself, and only for the method being activated.
        if method is PayoutMethod.UPI:
            profile.payout_display = decrypt(profile.payout_vpa_encrypted)
        else:
            profile.payout_display = self._mask_account(
                decrypt(profile.payout_account_number_encrypted)
            )
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
