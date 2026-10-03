"""
Provider service: profile management, KYC field encryption, online-status
toggling, and geospatial discovery.
"""

import asyncio
import io
import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone

# SVC-307: type-only import so the ``verifier`` param signature can name the
# class without pulling google-genai + ADC discovery into the module import
# path (that would fail at test collection time when creds aren't wired).
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import (
    AppException,
    DuplicateSubscription,
    InvalidPayoutMethod,
    ServiceAreaMissing,
    ServiceNotFound,
    SubscriptionNotFound,
    UnauthorizedAction,
    UserNotFound,
)
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.booking_status_log import BookingStatusLog
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, PayoutMethod, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.models.user import User
from app.utils import storage
from app.utils.encryption import decrypt, encrypt
from app.utils.geo import build_point_wkt, distance_expression, st_dwithin_filter

if TYPE_CHECKING:
    from app.services.kyc_verifier import KYCVerifier

# Indian Standard Time — earnings windows are user-facing ("today", "this
# week", "this month") so they must respect the user's local calendar boundary,
# not UTC. Single-region for now; revisit when we have providers outside India.
_IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger(__name__)

EarningsRange = Literal["today", "week", "month"]


def professional_display_name(user_name: str | None, business_name: str | None = None) -> str:
    """A Pro's ``display_name``: the person's name, else the business name, else
    a fixed label. Never the phone number, which consumers would later see.

    The same fallback as SVC-339's backfill SQL.
    """
    for candidate in (user_name, business_name):
        if candidate and candidate.strip():
            return candidate.strip()
    return "Professional"


def _earnings_window_start_utc(range_: EarningsRange) -> datetime:
    """Convert a named range into the inclusive UTC start instant."""
    now_ist = datetime.now(_IST)
    if range_ == "today":
        start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_ == "week":
        # ISO week starts Monday — matches typical IN payroll conventions.
        start_ist = (now_ist - timedelta(days=now_ist.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:  # month
        start_ist = now_ist.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_ist.astimezone(UTC)


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
            # SVC-320: every SP gets its Pro-for-self at birth, in the same
            # transaction. SVC-339's migration only covered providers that
            # existed when it ran.
            self.db.add(await self._new_self_professional(profile))
            await self.db.flush()
        return profile, is_new

    async def ensure_self_professional(self, sp_id: uuid.UUID) -> Professional:
        """The SP's Pro-for-self, created if missing (SVC-320 PR 2).

        Every SP has one since PR 1 (signup creates it, the migration backfilled
        the rest), so a miss means a row made some other way: a test fixture, a
        manual insert. Accept must never fail on that, so it repairs it.
        """
        pro = await self.db.scalar(
            select(Professional).where(
                Professional.service_provider_id == sp_id,
                Professional.user_id == sp_id,
            )
        )
        if pro is not None:
            return pro
        profile = await self.get_profile(sp_id)
        if profile is None:
            raise UserNotFound(sp_id)
        logger.warning("provider.self_pro_missing sp=%s; creating it", sp_id)
        pro = await self._new_self_professional(profile)
        self.db.add(pro)
        await self.db.flush()
        return pro

    async def _new_self_professional(self, profile: ProviderProfile) -> Professional:
        user = await self.db.get(User, profile.user_id)
        return Professional(
            service_provider_id=profile.user_id,
            user_id=profile.user_id,
            display_name=professional_display_name(
                user.name if user else None, profile.business_name
            ),
        )

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
        languages: list[str] | None = None,
        business_name: str | None = None,
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
        #
        # SVC-189 (G16): payout destinations can only be added once KYC is
        # verified. Before this, a provider could register, skip KYC entirely,
        # and land money in `PENDING` settlements that an admin might then pay
        # out without knowing KYC never happened. Non-payout fields on this
        # request (KYC docs, service area, languages) stay writable — the gate
        # is scoped to *payout* fields only.
        if payout_vpa is not None or payout_account_number is not None or payout_ifsc is not None:
            self._require_kyc_verified(
                profile,
                "Payout details can only be added once KYC is verified.",
            )
            # SVC-192 (G33): any destination write invalidates the cached
            # RazorpayX fund account — the fund_account_id points at the
            # previous destination and using it after a change would send
            # money to the old VPA/bank. Contact id represents the person and
            # stays valid across destination edits.
            profile.razorpayx_fund_account_id = None
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

        # Already validated + de-duplicated by the schema; an empty list clears it.
        if languages is not None:
            profile.languages = languages

        # SVC-336: already trimmed and non-blank from the schema. Not KYC-gated —
        # onboarding sets it in step 1, before KYC is submitted.
        if business_name is not None:
            profile.business_name = business_name

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

        # provider-photos/* is served public-read by the bucket policy on
        # eazyserv-uploads (SVC-338); kyc/* stays private. The unguessable key is
        # what keeps photos from being enumerated — the policy grants no ListBucket.
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

    async def update_business_image(
        self,
        user_id: uuid.UUID,
        image_bytes: bytes,
        content_type: str,
        ext: str,
    ) -> ProviderProfile:
        """Store/replace the provider's business image (logo) in S3 (SVC-336).

        Same shape as ``update_photo``, and deliberately the same public-read
        prefix: a logo is shown to consumers exactly like a profile photo, so it
        needs no bucket-policy change (SVC-338). The ``business-`` file-name prefix
        keeps logos distinguishable from profile photos in the bucket.
        """
        profile, _ = await self.get_or_create_profile(user_id)

        key = f"provider-photos/{user_id}/business-{uuid.uuid4().hex}.{ext}"

        # boto3 is sync — offload to a thread so the event loop stays responsive.
        try:
            url = await asyncio.to_thread(
                storage.upload_file,
                io.BytesIO(image_bytes),
                key,
                content_type=content_type,
            )
        except Exception as exc:
            raise AppException(
                f"Business image upload failed: {exc}",
                status_code=502,
            ) from exc

        profile.business_image_url = url
        await self.db.flush()
        return profile

    @staticmethod
    def _require_kyc_verified(profile: ProviderProfile, reason: str) -> None:
        """Refuse a payout-related mutation unless KYC is verified (SVC-189 / G16).

        Kept as a shared helper so every path that writes or activates payout
        details enforces the same rule (update_profile's payout branch,
        set_active_payout_method, and the payout side in PayoutService). One
        rule, one message shape — a caller cannot slip through by using a
        different entry point.
        """
        if profile.kyc_status is not KYCStatus.VERIFIED:
            raise AppException(
                f"{reason} Current KYC status: {profile.kyc_status.value}.",
                status_code=400,
            )

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
        # SVC-192 (G33): removing all payout data invalidates both cached
        # RazorpayX ids. If the provider later adds destinations back, they'll
        # get a fresh contact + fund account — safer than reusing ids for a
        # relationship that was explicitly torn down (removal is the one
        # user-driven path where the person could conceptually change too).
        profile.razorpayx_fund_account_id = None
        profile.razorpayx_contact_id = None
        await self.db.flush()
        return profile

    async def set_active_payout_method(
        self, user_id: uuid.UUID, method: PayoutMethod
    ) -> ProviderProfile:
        """Switch payouts to an already-stored method, without re-entering it.

        Rejects a method with nothing on file — activating a destination we cannot
        actually pay to would fail later at payout time, when the provider is no
        longer around to correct it.

        "Is it stored?" is answered by ``ProviderProfile.payout_methods_stored``, the
        same property the profile response exposes (SVC-152). Deliberately one shared
        rule: if this check and what we advertise as switchable could drift, the
        frontend would offer a switch that 400s.
        """
        profile, _ = await self.get_or_create_profile(user_id)

        if method not in profile.payout_methods_stored:
            raise InvalidPayoutMethod(
                "No UPI ID on file to switch to"
                if method is PayoutMethod.UPI
                else "No bank account on file to switch to"
            )

        # SVC-189 (G16): activating a payout destination is the last step
        # before money can flow — subject to the same KYC gate as writing new
        # details. Removing the active method is deliberately NOT gated (see
        # remove_payout_method): a provider can always withdraw their PII.
        self._require_kyc_verified(
            profile,
            "Payout method can only be activated once KYC is verified.",
        )

        # SVC-192 (G33): switching UPI ↔ bank points at a different destination,
        # so the cached fund_account_id (bound to the previous one) is no
        # longer valid. Contact stays — same person.
        if profile.payout_method is not method:
            profile.razorpayx_fund_account_id = None

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
        # SVC-257: going online with no service_area_center makes the provider
        # invisible to the PostGIS dispatch query — they'd appear active in
        # the app while receiving zero offers, and the silent failure only
        # surfaces via missed jobs. Refuse at the wire with a code the app
        # can catch to nudge the provider through the missing wizard step.
        # Going OFFLINE is always allowed regardless of geo state.
        if is_online and profile.service_area_center is None:
            raise ServiceAreaMissing()
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

        return [{**row.ProviderProfile.__dict__, "distance_km": row.distance_km} for row in rows]

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
            raise UnauthorizedAction("Only verified providers can subscribe to services")

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

    async def list_subscriptions(self, provider_user_id: uuid.UUID) -> list[ProviderServiceModel]:
        """Return the provider's current service subscriptions."""
        result = await self.db.execute(
            select(ProviderServiceModel).where(ProviderServiceModel.provider_id == provider_user_id)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Rating update (called by review_service after new review)
    # ------------------------------------------------------------------

    async def refresh_avg_rating(self, provider_user_id: uuid.UUID, new_rating: int) -> None:
        """Incrementally update avg_rating using a running weighted average."""
        profile = await self.get_profile(provider_user_id)
        if not profile:
            return
        n = profile.total_completed_jobs or 0
        if n == 0:
            profile.avg_rating = float(new_rating)
        else:
            profile.avg_rating = round((profile.avg_rating * n + new_rating) / (n + 1), 2)
        await self.db.flush()

    # ------------------------------------------------------------------
    # KYC submission + admin review (SC-31)
    # ------------------------------------------------------------------

    async def submit_kyc(
        self,
        user_id: uuid.UUID,
        aadhaar_front_bytes: bytes,
        aadhaar_front_content_type: str,
        aadhaar_front_ext: str,
        aadhaar_back_bytes: bytes,
        aadhaar_back_content_type: str,
        aadhaar_back_ext: str,
        selfie_bytes: bytes,
        selfie_content_type: str,
        selfie_ext: str,
        verifier: "KYCVerifier | None" = None,
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
        aadhaar_front_key = f"kyc/{user_id}/aadhaar-front-{uuid.uuid4().hex}.{aadhaar_front_ext}"
        aadhaar_back_key = f"kyc/{user_id}/aadhaar-back-{uuid.uuid4().hex}.{aadhaar_back_ext}"
        selfie_key = f"kyc/{user_id}/selfie-{uuid.uuid4().hex}.{selfie_ext}"

        # boto3 is sync — offload to a thread so the event loop stays responsive.
        try:
            await asyncio.to_thread(
                storage.upload_file,
                io.BytesIO(aadhaar_front_bytes),
                aadhaar_front_key,
                content_type=aadhaar_front_content_type,
            )
            await asyncio.to_thread(
                storage.upload_file,
                io.BytesIO(aadhaar_back_bytes),
                aadhaar_back_key,
                content_type=aadhaar_back_content_type,
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
            "aadhaar_front_key": aadhaar_front_key,
            "aadhaar_back_key": aadhaar_back_key,
            "selfie_key": selfie_key,
            "aadhaar_front_content_type": aadhaar_front_content_type,
            "aadhaar_back_content_type": aadhaar_back_content_type,
            "selfie_content_type": selfie_content_type,
        }
        profile.kyc_status = KYCStatus.SUBMITTED
        profile.kyc_submitted_at = datetime.now(UTC)
        # Clear any prior rejection notes / reviewer stamps so the admin UI
        # doesn't surface stale ones from an earlier reject that this
        # submission is trying to fix.
        profile.kyc_reviewed_at = None
        profile.kyc_rejection_reason = None
        profile.kyc_reviewed_by = None
        profile.kyc_ai_verdict = None

        await self.db.flush()

        # SVC-307: score the submission with the AI verifier and (optionally)
        # auto-transition. Runs after the S3 uploads succeed, on the same
        # request thread — the LLM call is ~15s worst case, which fits in
        # the submit_kyc request. The verifier itself never raises: on any
        # LLM failure it returns a ``needs_review`` verdict flagged
        # ``llm_unavailable``, so the submission still lands in the admin
        # queue rather than 500'ing the provider.
        await self._score_and_maybe_auto_approve(profile, verifier=verifier)
        await self.db.flush()
        return profile

    async def _score_and_maybe_auto_approve(
        self,
        profile: ProviderProfile,
        *,
        verifier: "KYCVerifier | None" = None,
    ) -> None:
        """Run the KYC verifier over the just-uploaded docs and apply outcome.

        Extracted from ``submit_kyc`` so the auto-transition rule lives in one
        place; passing an injected ``verifier`` keeps the unit tests off the
        real Vertex client. See ``app.services.kyc_verifier`` for the LLM
        call itself.
        """
        from app.services.kyc_verifier import (
            KYCVerdict,
            KYCVerifier,
            get_auto_approve_enabled,
        )

        # Re-read the bytes from S3 so we score exactly what we stored — this
        # is one round-trip on the submit path, in exchange for not carrying
        # the raw bytes through two service methods. boto3 is sync → thread.
        docs = profile.kyc_docs or {}
        try:
            front_bytes, back_bytes, selfie_bytes = await asyncio.gather(
                asyncio.to_thread(storage.download_bytes, docs["aadhaar_front_key"]),
                asyncio.to_thread(storage.download_bytes, docs["aadhaar_back_key"]),
                asyncio.to_thread(storage.download_bytes, docs["selfie_key"]),
            )
        except Exception as exc:  # noqa: BLE001 — treated as verifier-unavailable
            verdict = KYCVerdict(
                outcome="needs_review",
                confidence=0.0,
                extracted={},
                flags=["docs_unreadable"],
                raw_response={"error": str(exc)},
            )
        else:
            # Fetch stated name off the user row — the LLM cross-checks against
            # this to catch aadhaar-under-a-different-name submissions. DOB
            # isn't captured during onboarding today (no field on users), so
            # the DOB check silently no-ops until that changes.
            user = await self.db.get(User, profile.user_id)
            stated_name = (user.name if user else "") or ""
            v = verifier or KYCVerifier()
            verdict = await v.verify(
                front_bytes=front_bytes,
                back_bytes=back_bytes,
                selfie_bytes=selfie_bytes,
                stated_name=stated_name,
                stated_dob=None,
            )

        profile.kyc_ai_verdict = verdict.model_dump()

        # Auto-transition is gated on: (1) the SSM flag being on AND
        # (2) an ``auto_approve`` outcome. Rejects NEVER auto-transition —
        # the ticket's cheap safety valve for LLM false negatives, so a
        # reject verdict always lands in the admin queue with the flags
        # visible rather than short-circuiting to REJECTED.
        settings = get_settings()
        if verdict.outcome == "auto_approve" and get_auto_approve_enabled():
            profile.kyc_status = KYCStatus.VERIFIED
            profile.kyc_reviewed_at = datetime.now(UTC)
            profile.kyc_reviewed_by = uuid.UUID(settings.KYC_VERIFIER_BOT_USER_ID)
            # Structured event so we can grep the audit trail for AI-driven
            # transitions later — "who reviewed this" is answerable via the
            # kyc_reviewed_by FK, but "why did the AI say auto_approve" needs
            # the flags + confidence at the moment of decision.
            import structlog

            structlog.get_logger(__name__).info(
                "kyc_ai_auto_approved",
                user_id=str(profile.user_id),
                confidence=verdict.confidence,
                flags=verdict.flags,
            )

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
        profile.kyc_reviewed_at = datetime.now(UTC)
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
        profile.kyc_reviewed_at = datetime.now(UTC)
        profile.kyc_rejection_reason = reason
        await self.db.flush()
        return profile

    async def get_kyc_review_payload(self, user_id: uuid.UUID) -> dict:
        """
        Admin-only: full KYC review payload — profile metadata + presigned URLs
        for the uploaded docs.

        URLs are minted on demand (1h TTL) so the admin UI can render the
        Aadhaar / selfie inline without ever making the bucket public. Joined
        with the User row so the admin sees who they're reviewing without a
        second round-trip.
        """
        from sqlalchemy.orm import joinedload

        result = await self.db.execute(
            select(ProviderProfile)
            .where(ProviderProfile.user_id == user_id)
            .options(joinedload(ProviderProfile.user))
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise UserNotFound(user_id)

        docs = profile.kyc_docs or {}
        aadhaar_key = docs.get("aadhaar_key")
        selfie_key = docs.get("selfie_key")

        # boto3 presign is sync — to_thread to keep the loop responsive even
        # though presigning itself is local (no network call).
        aadhaar_url = (
            await asyncio.to_thread(storage.get_presigned_url, aadhaar_key) if aadhaar_key else None
        )
        selfie_url = (
            await asyncio.to_thread(storage.get_presigned_url, selfie_key) if selfie_key else None
        )

        return {
            "user_id": profile.user_id,
            "name": profile.user.name,
            "phone": profile.user.phone,
            "kyc_status": profile.kyc_status,
            "kyc_submitted_at": profile.kyc_submitted_at,
            "kyc_reviewed_at": profile.kyc_reviewed_at,
            "kyc_rejection_reason": profile.kyc_rejection_reason,
            "aadhaar_url": aadhaar_url,
            "selfie_url": selfie_url,
            # SVC-307: full AI verdict JSONB. Present when the submission was
            # scored, None for pre-SVC-307 rows that were never scored.
            "ai_verdict": profile.kyc_ai_verdict,
        }

    # ------------------------------------------------------------------
    # Earnings (SC-33)
    # ------------------------------------------------------------------

    @staticmethod
    def _earned_filter() -> tuple:
        """What counts as money a provider has **earned** — the single definition.

        Deliberately distinct from ``PayoutService._owed_filter``, which answers a
        different question: what the platform still has to *send*. The gap is cash.
        A cash job is earned — the provider collected it in hand at the door — but
        never owed, because paying it again would send them the money twice.

        Reusing the payout filter here would be the tempting mistake: it looks like
        the authoritative definition of provider money, and it is, for payouts. On
        an earnings figure it would silently omit every cash job — understating what
        the provider made, in the platform's favour, with nothing on screen to
        explain the gap.

        Shared by :meth:`get_earnings` and :meth:`get_earnings_summary` so a window
        and the lifetime total cannot disagree about which bookings count.
        """
        return (
            Booking.status == BookingStatus.COMPLETED,
            Booking.payment_status != PaymentStatus.REFUNDED,
        )

    async def get_earnings_summary(self, provider_user_id: uuid.UUID) -> dict:
        """Lifetime payout total and completed count — no per-booking breakdown.

        Separate from :meth:`get_earnings` rather than a fourth ``range`` value,
        because that method returns one row per booking. Over a window that is a
        handful; over a lifetime it is every job the provider has ever done, and
        Provider Home would download all of it to render a single number. This
        returns two scalars whatever the history looks like.

        No ``BookingStatusLog`` join, unlike the windowed query. That join exists
        only to bucket a booking by the instant it completed, which a lifetime total
        has no need for — and it is an inner join on ``to_status == COMPLETED``, so a
        booking carrying two COMPLETED log rows would be counted **twice**. A
        window hides that; a lifetime sum would show it as a wrong total.

        Tips are included for the same reason they are in the windowed figure: a tip
        is 100% provider-side (see ``Booking.tip_amount``).
        """
        stmt = select(
            func.coalesce(func.sum(Booking.provider_payout + Booking.tip_amount), 0),
            func.count(Booking.id),
        ).where(Booking.provider_id == provider_user_id, *self._earned_filter())

        total, count = (await self.db.execute(stmt)).one()
        return {
            # A provider with no completed jobs gets 0.0, not null — the row on
            # Home reads "₹0" rather than rendering an empty space that looks
            # like a failed fetch.
            "lifetime_payout": round(float(total), 2),
            "completed_count": count,
        }

    async def get_earnings(
        self,
        provider_user_id: uuid.UUID,
        range_: EarningsRange,
    ) -> dict:
        """
        Sum the provider's payouts from COMPLETED bookings inside the named
        IST calendar window, plus the per-booking breakdown.

        Completion timestamp comes from `booking_status_logs` (the precise
        moment the booking transitioned to COMPLETED), not `bookings.updated_at`
        — that gets bumped by subsequent payment/webhook updates and would
        bucket a booking into the wrong day.

        Refunded payments are excluded — that's no longer money the provider
        earns. Cancelled bookings naturally fall out via the COMPLETED filter.

        Caching: spec asks for ~30s per provider; intentionally NOT done here
        yet. See backlog item — adds a Redis fixture story to every test and
        the call rate at beta scale is negligible. Easy follow-up.
        """
        start_utc = _earnings_window_start_utc(range_)

        # ``Booking.service_id`` was the single-service pointer that a booking
        # used to carry; a booking now holds a cart via ``booking_items``.
        # The earnings dashboard shows one line per booking, so this
        # aggregates the service names into a single comma-joined string
        # ("Deep Clean, Sofa Clean") using Postgres' string_agg. A separate
        # per-item earnings breakdown is a followup — for now the provider
        # sees the same view as before, just with multi-service carts
        # rendered as "svc1, svc2" instead of losing the second name.
        from app.models.booking_item import BookingItem

        service_names_subq = (
            select(func.string_agg(Service.name, ", "))
            .select_from(BookingItem)
            .join(Service, Service.id == BookingItem.service_id)
            .where(BookingItem.booking_id == Booking.id)
            .correlate(Booking)
            .scalar_subquery()
        )

        # Payout to the provider is the base commission split PLUS any
        # consumer tip — tips are 100% pro-side (see Booking.tip_amount).
        # Selected as an expression so both the per-row and total sums stay
        # consistent without a Python re-derive.
        effective_payout = (Booking.provider_payout + Booking.tip_amount).label("payout")

        stmt = (
            select(
                Booking.id,
                effective_payout,
                Booking.tip_amount,
                service_names_subq.label("service_name"),
                BookingStatusLog.created_at.label("completed_at"),
                # SVC-303: where this job's money is. Plain columns on the
                # booking — no extra join, so the cost of the query is
                # unchanged. A per-job *paid date* would need a join onto
                # payout_audits through settlement_payout_id, which is both
                # unindexed and duplicated (a reconciled payout writes two
                # rows sharing one payout_id), so it would double this
                # window's total. The dates live on the settlement statement.
                Booking.settlement_status,
                Booking.payment_method,
                Booking.settlement_failure_reason,
            )
            .join(
                BookingStatusLog,
                (BookingStatusLog.booking_id == Booking.id)
                & (BookingStatusLog.to_status == BookingStatus.COMPLETED.value),
            )
            .where(Booking.provider_id == provider_user_id)
            # Shared with the lifetime summary, so a window and the all-time
            # total cannot disagree about which bookings count as earned.
            .where(*self._earned_filter())
            .where(BookingStatusLog.created_at >= start_utc)
            .order_by(BookingStatusLog.created_at.desc())
        )

        rows = (await self.db.execute(stmt)).all()

        bookings = [
            {
                "booking_id": row.id,
                "service_name": row.service_name,
                "completed_at": row.completed_at,
                "payout_amount": row.payout,
                "tip_amount": row.tip_amount,
                "settlement_status": row.settlement_status,
                "payment_method": row.payment_method,
                "settlement_failure_reason": row.settlement_failure_reason,
            }
            for row in rows
        ]
        return {
            "range": range_,
            "total_payout": round(sum(row.payout for row in rows), 2),
            "completed_count": len(rows),
            "bookings": bookings,
        }
