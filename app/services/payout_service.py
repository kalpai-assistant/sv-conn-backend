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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.booking import Booking, BookingStatus, SettlementStatus
from app.models.provider_profile import ProviderProfile
from app.models.payout_audit import PayoutAudit
from app.exceptions import PaymentError, UserNotFound
from app.utils.encryption import decrypt
from app.utils.razorpayx_client import create_payout, list_payouts

logger = logging.getLogger(__name__)

# RazorpayX terminal payout statuses → our settlement state, for reconciliation.
_RECONCILE_MAP = {
    "processed": SettlementStatus.COMPLETED,
    "failed": SettlementStatus.FAILED,
    "reversed": SettlementStatus.REVERSED,
}


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

        ``admin_user_id`` is the admin triggering the payout; every attempt is
        recorded in ``payout_audits`` — an ``attempting`` row is committed before the
        RazorpayX call and resolved to the final outcome, so a crash mid-flight still
        leaves a locatable record for reconciliation.
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
        # existing payout rather than creating a duplicate. The same key is also sent
        # as the RazorpayX reference_id so reconciliation can map a payout back to its
        # exact batch.
        idempotency_key = self._batch_idempotency_key(bookings)

        logger.info(
            "payout.attempt admin=%s provider=%s amount_paise=%s bookings=%s",
            admin_user_id, provider_id, amount_paise, len(bookings),
        )

        # Breadcrumb committed BEFORE the RazorpayX call, in its own transaction, so
        # a crash after the money moves but before this request commits still leaves
        # a locatable record (keyed by reference_id) for reconciliation to resolve.
        audit_id = await self._record_attempting_audit(
            admin_user_id, provider_id, amount_paise, len(bookings), idempotency_key
        )

        try:
            payout = await create_payout(
                amount_paise=amount_paise,
                idempotency_key=idempotency_key,
                reference_id=idempotency_key,
                vpa=vpa,
                account_number=account_number,
                ifsc=ifsc,
            )
        except Exception as exc:
            # The payout call failed (after retries). Resolve the breadcrumb to
            # ``failed`` in its own transaction so it survives the request rollback,
            # then re-raise for the caller to surface to the admin.
            logger.error(
                "payout.failed admin=%s provider=%s amount_paise=%s error=%s",
                admin_user_id, provider_id, amount_paise, exc,
            )
            await self._resolve_audit(audit_id, outcome="failed", error=str(exc))
            raise

        # The payout has been created at RazorpayX; record it on the bookings. If
        # this write fails, the safe retry above recovers (same key -> same payout).
        payout_id = payout.get("id")
        status = payout.get("status")
        for booking in bookings:
            booking.settlement_status = SettlementStatus.PROCESSING
            booking.settlement_payout_id = payout_id

        # Resolve the breadcrumb to the real outcome + payout id.
        await self._resolve_audit(
            audit_id, outcome=status or "initiated", payout_id=payout_id
        )
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

    async def requeue_settlements(
        self, provider_id: UUID, admin_user_id: UUID, *, from_status: SettlementStatus
    ) -> dict:
        """
        Reset a provider's stranded settlements back to PENDING so they can be paid
        again. ``from_status`` must be FAILED or REVERSED.

        Both states are excluded from ``create_provider_payout`` (which only selects
        PENDING), so without this they're stranded. Re-payment is a deliberate manual
        admin action, taken *after* the root cause is fixed:

        - **FAILED** — the payout never reached the provider (bad bank details, KYC
          hold, ...); once fixed, re-queue to try again.
        - **REVERSED** — the payout reached the provider and was then clawed back.
          This often signals a KYC/compliance problem, so **confirm the provider is
          genuinely still owed before re-queuing** — re-paying a reversal you
          shouldn't sends money that will likely bounce again.

        The old payout id is cleared so the next payout links cleanly. Records an
        admin audit row and returns how many were re-queued.
        """
        if from_status not in (SettlementStatus.FAILED, SettlementStatus.REVERSED):
            raise PaymentError(
                f"Cannot re-queue settlements from {from_status.value}"
            )

        result = await self.db.execute(
            select(Booking)
            .where(
                Booking.provider_id == provider_id,
                Booking.status == BookingStatus.COMPLETED,
                Booking.settlement_status == from_status,
            )
            .with_for_update(skip_locked=True)
        )
        stuck = list(result.scalars().all())
        if not stuck:
            raise PaymentError(
                f"No {from_status.value} settlements to re-queue for this provider"
            )

        for booking in stuck:
            booking.settlement_status = SettlementStatus.PENDING
            booking.settlement_payout_id = None

        amount_paise = int(sum(b.provider_payout for b in stuck) * 100)
        self.db.add(PayoutAudit(
            admin_user_id=admin_user_id,
            provider_id=provider_id,
            source="admin",
            amount_paise=amount_paise,
            booking_count=len(stuck),
            payout_id=None,
            outcome="requeued",
        ))
        await self.db.flush()

        logger.info(
            "payout.requeue admin=%s provider=%s from=%s bookings=%s",
            admin_user_id, provider_id, from_status.value, len(stuck),
        )
        return {
            "requeued": len(stuck),
            "from_status": from_status.value,
            "provider_id": str(provider_id),
        }

    async def handle_payout_webhook(self, event: str, payload: dict) -> None:
        """
        Move a payout's settlements based on its outcome.

        ``payout.processed`` → COMPLETED, ``payout.failed`` → FAILED,
        ``payout.reversed`` → REVERSED (completed money clawed back; also alerts).
        Bookings are matched on the RazorpayX payout id (``settlement_payout_id``),
        so concurrent payouts settle independently. Idempotent: only bookings in a
        transitionable state for that payout are touched.
        """
        # Terminal payout events we act on. Everything else (payout.updated,
        # transaction.*) is a legitimate no-op — acknowledge with a breadcrumb.
        terminal = {
            "payout.processed": SettlementStatus.COMPLETED,
            "payout.failed": SettlementStatus.FAILED,
            "payout.reversed": SettlementStatus.REVERSED,
        }
        if event not in terminal:
            logger.info("Ignoring non-terminal RazorpayX webhook event: %s", event)
            return

        entity = payload.get("payout", {}).get("entity", {})
        payout_id = entity.get("id")
        if not payout_id:
            # A terminal event with no payout id is malformed — we can't map it to
            # any settlement. Surface it instead of failing silently.
            logger.error("RazorpayX %s webhook missing payout.entity.id", event)
            raise PaymentError("Payout webhook missing payout id")

        new_status = terminal[event]
        # processed/failed transition a PROCESSING settlement; a reversal claws back a
        # payout that had already COMPLETED (and defensively one still PROCESSING).
        from_statuses = (
            (SettlementStatus.PROCESSING, SettlementStatus.COMPLETED)
            if event == "payout.reversed"
            else (SettlementStatus.PROCESSING,)
        )

        result = await self.db.execute(
            select(Booking).where(
                Booking.settlement_payout_id == payout_id,
                Booking.settlement_status.in_(from_statuses),
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
        if event == "payout.reversed":
            # Completed money was clawed back — always surface for a human to review.
            logger.error(
                "ALERT payout.reversed payout_id=%s reversed_bookings=%s",
                payout_id, len(settled),
            )

    async def reconcile_payouts(self, *, since_ts: int, until_ts: int) -> dict:
        """
        Reconcile our settlement state against RazorpayX's record of payouts.

        Detects and repairs drift left when a webhook is missed or when the create
        path crashed after RazorpayX moved the money but before we recorded it:

        - **Case A** — bookings linked to the payout (``settlement_payout_id``) but
          not yet in the terminal state (a missed/dropped webhook) → transition them.
        - **Case B** — no booking references the payout (the dual-write orphan) →
          find the batch via the ``reference_id`` breadcrumb and re-link it.

        Anything that can't be resolved is counted and alerted for a human. Returns a
        summary dict; the caller commits.
        """
        payouts = await list_payouts(from_ts=since_ts, to_ts=until_ts)
        report = {"checked": 0, "case_a": 0, "case_b": 0, "unresolved": 0}

        for payout in payouts:
            target = _RECONCILE_MAP.get(payout.get("status"))
            if target is None:
                continue  # non-terminal (queued/processing) — nothing to reconcile
            report["checked"] += 1

            repaired = await self._reconcile_linked(payout, target)
            if repaired is not None:
                report["case_a"] += repaired
                continue

            relinked = await self._reconcile_orphan(payout, target)
            if relinked > 0:
                report["case_b"] += relinked
            else:
                report["unresolved"] += 1
                logger.error(
                    "ALERT payout reconciliation unresolved payout_id=%s "
                    "reference_id=%s status=%s",
                    payout.get("id"), payout.get("reference_id"), payout.get("status"),
                )

        await self.db.flush()
        logger.info("payout.reconcile %s", report)
        return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _record_attempting_audit(
        admin_user_id: UUID,
        provider_id: UUID,
        amount_paise: int,
        booking_count: int,
        reference_id: str,
    ) -> UUID:
        """Write an ``attempting`` audit row in its own committed transaction.

        Idempotent on ``reference_id``: a retry of the same batch after a mid-flight
        crash produces the same key, so we reuse the existing row instead of writing a
        duplicate — the audit table stays a 1:1 ledger. Uses a fresh session (not
        ``self.db``) so the row survives the request's rollback. ``reference_id`` (the
        batch key) lets reconciliation map an orphaned payout back to this row.
        Returns the row's id.
        """
        async with AsyncSessionLocal() as session:
            existing = (
                await session.execute(
                    select(PayoutAudit.id).where(
                        PayoutAudit.reference_id == reference_id
                    )
                )
            ).scalars().first()
            if existing is not None:
                return existing

            audit = PayoutAudit(
                admin_user_id=admin_user_id,
                provider_id=provider_id,
                source="admin",
                amount_paise=amount_paise,
                booking_count=booking_count,
                reference_id=reference_id,
                payout_id=None,
                outcome="attempting",
            )
            session.add(audit)
            await session.commit()
            return audit.id

    @staticmethod
    async def _resolve_audit(
        audit_id: UUID,
        *,
        outcome: str,
        payout_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Resolve an ``attempting`` audit row to its final outcome.

        Own transaction (like the write above) so the success/failure outcome is
        recorded independently of the request's transaction. If no row matches
        ``audit_id`` (e.g. it was deleted out-of-band), the update silently no-ops and
        the breadcrumb would stay ``attempting`` forever — so we surface that as an
        error rather than swallow it. This runs after the money has moved, so we log
        (alert) rather than raise, which would fail the request post-payout.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                update(PayoutAudit)
                .where(PayoutAudit.id == audit_id)
                .values(
                    outcome=outcome,
                    payout_id=payout_id,
                    error=(error[:500] if error else None),
                )
            )
            await session.commit()
            if result.rowcount != 1:
                logger.error(
                    "ALERT payout audit resolve touched %s rows for audit_id=%s "
                    "(expected 1) — breadcrumb not updated",
                    result.rowcount, audit_id,
                )

    @staticmethod
    def _batch_idempotency_key(bookings: list[Booking]) -> str:
        """Stable key for a payout batch — a hash of its sorted booking ids."""
        ids = ",".join(sorted(str(b.id) for b in bookings))
        return "payout_" + hashlib.sha256(ids.encode()).hexdigest()[:32]

    async def _reconcile_linked(
        self, payout: dict, target: SettlementStatus
    ) -> int | None:
        """Case A: repair bookings already linked to this payout id.

        Transitions linked bookings not yet in ``target`` and returns how many were
        repaired (0 if already consistent). Returns ``None`` when no booking
        references this payout id — i.e. it's an orphan for Case B to handle.
        """
        payout_id = payout.get("id")
        linked = (
            await self.db.execute(
                select(Booking).where(Booking.settlement_payout_id == payout_id)
            )
        ).scalars().all()
        if not linked:
            return None

        repaired = [b for b in linked if b.settlement_status != target]
        for booking in repaired:
            booking.settlement_status = target
        if repaired:
            self._add_reconciliation_audit(
                provider_id=repaired[0].provider_id, payout=payout,
                target=target, count=len(repaired),
            )
        return len(repaired)

    async def _reconcile_orphan(self, payout: dict, target: SettlementStatus) -> int:
        """Case B: re-link a payout that no booking references.

        Maps the payout back to its batch via ``reference_id`` (the batch key) and
        the ``attempting`` audit breadcrumb, cross-checks the amount, and confirms
        the provider's still-PENDING settlements are exactly that batch before
        re-linking. Returns rows re-linked, or 0 if it can't be resolved.
        """
        reference_id = payout.get("reference_id")
        if not reference_id:
            return 0

        audit = (
            await self.db.execute(
                select(PayoutAudit).where(PayoutAudit.reference_id == reference_id)
            )
        ).scalars().first()
        if audit is None:
            return 0

        # Amount cross-check: never re-link a payout whose amount doesn't match what
        # we intended to pay for this batch.
        if payout.get("amount") is not None and payout["amount"] != audit.amount_paise:
            return 0

        # The batch must still be exactly the provider's PENDING settlements.
        pending = await self._pending_settlements(audit.provider_id)
        if not pending or self._batch_idempotency_key(pending) != reference_id:
            return 0

        payout_id = payout.get("id")
        for booking in pending:
            booking.settlement_status = target
            booking.settlement_payout_id = payout_id
        # Resolve the breadcrumb so a later run doesn't reprocess it.
        audit.outcome = target.value
        audit.payout_id = payout_id
        self._add_reconciliation_audit(
            provider_id=audit.provider_id, payout=payout,
            target=target, count=len(pending),
        )
        return len(pending)

    def _add_reconciliation_audit(
        self, *, provider_id: UUID, payout: dict, target: SettlementStatus, count: int
    ) -> None:
        """Record an automated (non-admin) reconciliation repair in payout_audits."""
        self.db.add(PayoutAudit(
            admin_user_id=None,
            provider_id=provider_id,
            source="reconciliation",
            amount_paise=payout.get("amount") or 0,
            booking_count=count,
            payout_id=payout.get("id"),
            reference_id=payout.get("reference_id"),
            outcome=target.value,
        ))

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
