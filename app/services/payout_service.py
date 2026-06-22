"""
Payout service: settle a provider's completed-but-unsettled bookings via a single
RazorpayX payout.

For beta this is triggered manually from the admin panel, on a weekly cadence.
Payment for each booking is collected up front to the platform account; this
service pays the provider their share afterwards.
"""
import hashlib
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.booking import Booking, BookingStatus, SettlementStatus
from app.models.provider_profile import ProviderProfile
from app.models.payout_audit import PayoutAudit
from app.exceptions import PaymentError, UserNotFound
from app.utils.encryption import decrypt
from app.utils.razorpayx_client import create_payout

logger = logging.getLogger(__name__)


class PayoutService:
    """Bulk provider payouts via RazorpayX."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_provider_payout(
        self, provider_id: UUID, admin_user_id: UUID
    ) -> dict:
        """
        Pay out all of a provider's completed bookings whose settlement is still
        PENDING, as a single RazorpayX payout.

        Sums the ``provider_payout`` of the eligible bookings, sends one payout to
        the provider's saved payout destination, and flips those bookings to
        PROCESSING. The webhook (payout.processed / payout.failed) later moves them
        to COMPLETED / FAILED.

        Idempotency: only PENDING bookings are selected, so a booking already being
        paid out (PROCESSING/COMPLETED) is never paid twice. The RazorpayX call also
        carries an idempotency key derived from the exact set of booking ids, so a
        retry after a partial failure returns the same payout instead of paying again.

        ``admin_user_id`` is the admin triggering the payout; every attempt is written
        to ``payout_audits`` (on success and on failure) for compliance.
        """
        profile = await self._get_provider_profile(provider_id)
        vpa, account_number, ifsc = self._decrypt_payout_details(profile)

        bookings = await self._pending_settlements(provider_id)
        if not bookings:
            raise PaymentError("No pending settlements for this provider")

        total_rupees = sum(b.provider_payout for b in bookings)
        amount_paise = int(total_rupees * 100)

        # Key derived from the exact batch (sorted booking ids), so the same batch
        # always produces the same key. If create_payout succeeds but the DB write
        # below fails, retrying this batch reuses the key and RazorpayX returns the
        # existing payout rather than creating a duplicate.
        idempotency_key = self._batch_idempotency_key(bookings)

        logger.info(
            "payout.attempt admin=%s provider=%s amount_paise=%s bookings=%s",
            admin_user_id, provider_id, amount_paise, len(bookings),
        )

        try:
            payout = await create_payout(
                amount_paise=amount_paise,
                idempotency_key=idempotency_key,
                reference_id=str(provider_id),
                vpa=vpa,
                account_number=account_number,
                ifsc=ifsc,
            )
        except Exception as exc:
            # The payout call failed (after retries). Record the failed attempt in
            # its own committed transaction so the audit survives the request
            # rollback, then re-raise for the caller to surface to the admin.
            logger.error(
                "payout.failed admin=%s provider=%s amount_paise=%s error=%s",
                admin_user_id, provider_id, amount_paise, exc,
            )
            await self._record_failed_audit(
                admin_user_id, provider_id, amount_paise, len(bookings), str(exc)
            )
            raise

        # The payout has been created at RazorpayX; record it on the bookings. If
        # this write fails, the safe retry above recovers (same key -> same payout).
        payout_id = payout.get("id")
        status = payout.get("status")
        for booking in bookings:
            booking.settlement_status = SettlementStatus.PROCESSING
            booking.settlement_payout_id = payout_id

        # Success audit shares this request's transaction, so it commits atomically
        # with the booking flips (committed by get_db on a clean response).
        self.db.add(PayoutAudit(
            admin_user_id=admin_user_id,
            provider_id=provider_id,
            amount_paise=amount_paise,
            booking_count=len(bookings),
            payout_id=payout_id,
            outcome=status or "initiated",
        ))
        await self.db.flush()

        logger.info(
            "payout.initiated admin=%s provider=%s payout_id=%s status=%s bookings=%s",
            admin_user_id, provider_id, payout_id, status, len(bookings),
        )

        return {
            "payout_id": payout_id,
            "status": status,
            "amount": amount_paise,
            "booking_count": len(bookings),
        }

    async def handle_payout_webhook(self, event: str, payload: dict) -> None:
        """
        Move a payout's settlements based on its outcome.

        ``payout.processed`` → COMPLETED, ``payout.failed`` → FAILED. Bookings are
        matched on the RazorpayX payout id (``settlement_payout_id``), so concurrent
        payouts for the same provider settle independently. Idempotent: only bookings
        still PROCESSING for that payout are touched.
        """
        # RazorpayX sends other event types to this endpoint too (payout.updated,
        # payout.reversed, transaction.*). They're a legitimate no-op for us, but
        # acknowledge them with a breadcrumb rather than swallowing them silently.
        if event not in ("payout.processed", "payout.failed"):
            logger.info("Ignoring non-terminal RazorpayX webhook event: %s", event)
            return

        entity = payload.get("payout", {}).get("entity", {})
        payout_id = entity.get("id")
        if not payout_id:
            # A payout.processed/failed event with no payout id is malformed — we
            # can't map it to any settlement. Surface it instead of failing silently.
            logger.error("RazorpayX %s webhook missing payout.entity.id", event)
            raise PaymentError("Payout webhook missing payout id")

        new_status = (
            SettlementStatus.COMPLETED
            if event == "payout.processed"
            else SettlementStatus.FAILED
        )

        result = await self.db.execute(
            select(Booking).where(
                Booking.settlement_payout_id == payout_id,
                Booking.settlement_status == SettlementStatus.PROCESSING,
            )
        )
        settled = result.scalars().all()
        for booking in settled:
            booking.settlement_status = new_status
        await self.db.flush()

        logger.info(
            "payout.webhook event=%s payout_id=%s settled=%s -> %s",
            event, payout_id, len(settled), new_status.value,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _record_failed_audit(
        admin_user_id: UUID,
        provider_id: UUID,
        amount_paise: int,
        booking_count: int,
        error: str,
    ) -> None:
        """Persist a failed payout attempt in its own transaction.

        Uses a fresh session (not ``self.db``) so the audit row commits even though
        the request's transaction is about to roll back on the re-raised error.
        """
        async with AsyncSessionLocal() as session:
            session.add(PayoutAudit(
                admin_user_id=admin_user_id,
                provider_id=provider_id,
                amount_paise=amount_paise,
                booking_count=booking_count,
                payout_id=None,
                outcome="failed",
                error=error[:500],
            ))
            await session.commit()

    @staticmethod
    def _batch_idempotency_key(bookings: list[Booking]) -> str:
        """Stable key for a payout batch — a hash of its sorted booking ids."""
        ids = ",".join(sorted(str(b.id) for b in bookings))
        return "payout_" + hashlib.sha256(ids.encode()).hexdigest()[:32]

    async def _get_provider_profile(self, provider_id: UUID) -> ProviderProfile:
        result = await self.db.execute(
            select(ProviderProfile).where(ProviderProfile.user_id == provider_id)
        )
        profile = result.scalar_one_or_none()
        if not profile:
            raise UserNotFound(provider_id)
        return profile

    def _decrypt_payout_details(
        self, profile: ProviderProfile
    ) -> tuple[str | None, str | None, str | None]:
        """Return (vpa, account_number, ifsc), preferring UPI when both are set."""
        vpa = decrypt(profile.payout_vpa_encrypted) if profile.payout_vpa_encrypted else None
        account_number = (
            decrypt(profile.payout_account_number_encrypted)
            if profile.payout_account_number_encrypted
            else None
        )
        ifsc = decrypt(profile.payout_ifsc_encrypted) if profile.payout_ifsc_encrypted else None

        if not vpa and not (account_number and ifsc):
            raise PaymentError("Provider has no payout details on file")
        return vpa, account_number, ifsc

    async def _pending_settlements(self, provider_id: UUID) -> list[Booking]:
        # Lock the selected rows for the duration of the transaction so two
        # concurrent payouts for the same provider can't both read the same
        # PENDING bookings and issue divergent batches (→ two payouts, double-pay).
        # skip_locked: a second concurrent click doesn't block — it skips the
        # locked rows, finds nothing PENDING, and raises "No pending settlements".
        result = await self.db.execute(
            select(Booking)
            .where(
                Booking.provider_id == provider_id,
                Booking.status == BookingStatus.COMPLETED,
                Booking.settlement_status == SettlementStatus.PENDING,
            )
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())
