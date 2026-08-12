"""
Payout service: settle a provider's completed-but-unsettled bookings via a single
RazorpayX payout.

For beta this is triggered manually from the admin panel, on a weekly cadence.
Payment for each booking is collected up front to the platform account; this
service pays the provider their share afterwards.
"""

import hashlib
import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.exceptions import PaymentError, UserNotFound
from app.models.booking import (
    Booking,
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
    SettlementStatus,
)
from app.models.payout_audit import PayoutAudit
from app.models.provider_profile import PayoutMethod, ProviderProfile
from app.models.user import User
from app.utils.encryption import decrypt
from app.utils.money import to_paise
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

    # The admin list is capped, not paginated (SVC-144): the payouts screen answers
    # "who do we owe right now", it isn't a browsable index.
    PENDING_SETTLEMENTS_LIMIT = 200

    def __init__(self, db: AsyncSession):
        self.db = db
        # Provider pushes held until the transaction that justifies them has
        # committed — see ``_buffer_notification``. Drained by the caller:
        # ``dispatch_pending_notifications`` (request paths, via BackgroundTasks) or
        # ``dispatch_notifications`` (the reconcile Celery task, post-commit).
        self._pending_notifications: list[dict] = []

    async def list_pending_settlements(self, *, limit: int = PENDING_SETTLEMENTS_LIMIT) -> dict:
        """Providers with money owed, aggregated one row per provider.

        Powers the admin payouts screen: each row is a provider the platform owes,
        with the batch total and booking count that ``create_provider_payout`` would
        settle if the admin pressed Pay out. Ordered largest debt first.

        Shares ``_owed_filter`` with the payout path so the amount shown and the
        amount paid cannot drift. Unlike ``_pending_settlements`` this takes **no row
        locks** — that helper is about to spend money, this one is rendering a page,
        and locking every provider's bookings to do so would be wrong.

        Capped at ``limit`` rows; ``total`` is the true provider count so the caller
        can tell the admin the list is incomplete.

        Rows and total come from **one** query. Counting separately would read two
        snapshots under READ COMMITTED, and a payout committing between them could
        report a ``total`` lower than the rows already returned — "showing 200 of
        150". ``COUNT(*) OVER ()`` is evaluated after GROUP BY and before LIMIT, so
        it counts provider groups pre-cap on the same snapshot as the rows.
        """
        # ProviderProfile is 1:1 with the user and grouped away, so aggregate it.
        # bool_or over the (at most one) profile row = "has an active destination".
        #
        # Mirrors ProviderProfile.payout_set, which is a plain @property and cannot
        # be evaluated in SQL. Since SVC-148 both are keyed on payout_method, so the
        # two definitions are trivially the same — and the empty-string mismatch
        # between Python truthiness and SQL IS NOT NULL that this had to work around
        # no longer exists, because an enum column cannot hold "".
        payout_set = func.bool_or(ProviderProfile.payout_method.is_not(None))
        # SVC-39 G23: include tip_amount — a tip is 100% provider and gets paid
        # out with the base commission split. Both columns are Numeric post-G31,
        # so the sum stays exact.
        total_rupees = func.sum(Booking.provider_payout + Booking.tip_amount)

        rows = (
            await self.db.execute(
                select(
                    User.id,
                    User.name,
                    User.phone,
                    User.is_active,
                    func.count(Booking.id),
                    total_rupees,
                    payout_set,
                    func.count().over().label("total_providers"),
                )
                .select_from(Booking)
                .join(User, User.id == Booking.provider_id)
                # LEFT join: a provider can have completed bookings but no profile row
                # at all. An inner join would drop them silently — hiding money owed.
                # They surface with payout_set=False, which is exactly what the admin
                # needs to see.
                .outerjoin(ProviderProfile, ProviderProfile.user_id == User.id)
                .where(*self._owed_filter())
                .group_by(User.id, User.name, User.phone, User.is_active)
                .order_by(total_rupees.desc())
                .limit(limit)
            )
        ).all()

        providers = [
            {
                "provider_id": provider_id,
                "name": name,
                "phone": phone,
                "is_active": is_active,
                "pending_booking_count": booking_count,
                # Same rupees→paise conversion the payout path uses, so the figure
                # in the confirmation dialog matches what is actually sent.
                "pending_payout_paise": to_paise(rupees),
                "payout_set": bool(has_payout),
            }
            for provider_id, name, phone, is_active, booking_count, rupees, has_payout, _ in rows
        ]

        # The window count is identical on every row; no rows means nothing is owed.
        total = rows[0].total_providers if rows else 0

        return {
            "providers": providers,
            "total": total,
            "truncated": total > len(providers),
        }

    async def create_provider_payout(self, provider_id: UUID, admin_user_id: UUID) -> dict:
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

        # Decimal throughout: the columns are Numeric, so this sum is exact and
        # to_paise rounds half up instead of dropping a paisa (SVC-150).
        # ``_owed_amount`` folds in tip_amount too (SVC-39 G23).
        total_rupees = sum((self._owed_amount(b) for b in bookings), Decimal("0"))
        amount_paise = to_paise(total_rupees)

        # Key derived from the exact batch (sorted booking ids), so the same batch
        # always produces the same key. If create_payout succeeds but the DB write
        # below fails, retrying this batch reuses the key and RazorpayX returns the
        # existing payout rather than creating a duplicate. The same key is also sent
        # as the RazorpayX reference_id so reconciliation can map a payout back to its
        # exact batch.
        idempotency_key = self._batch_idempotency_key(bookings)

        logger.info(
            "payout.attempt admin=%s provider=%s amount_paise=%s bookings=%s",
            admin_user_id,
            provider_id,
            amount_paise,
            len(bookings),
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
                admin_user_id,
                provider_id,
                amount_paise,
                exc,
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
            # Fresh attempt — wipe any prior rejection/failure reason so the field
            # only ever reflects the current cycle.
            booking.settlement_failure_reason = None

        # Resolve the breadcrumb to the real outcome + payout id.
        await self._resolve_audit(audit_id, outcome=status or "initiated", payout_id=payout_id)
        await self.db.flush()

        logger.info(
            "payout.initiated admin=%s provider=%s payout_id=%s status=%s bookings=%s",
            admin_user_id,
            provider_id,
            payout_id,
            status,
            len(bookings),
        )
        # Buffered, not sent: this transaction has not committed yet. RazorpayX has
        # already accepted the payout, so if the commit fails the money moved but we
        # have no record of it — and a "money on the way" push for a payout the DB
        # never recorded cannot be taken back. The caller drains after commit.
        self._buffer_notification(
            provider_id, SettlementStatus.PROCESSING.value, amount_paise, payout_id
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
            raise PaymentError(f"Cannot re-queue settlements from {from_status.value}")

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
            raise PaymentError(f"No {from_status.value} settlements to re-queue for this provider")

        for booking in stuck:
            booking.settlement_status = SettlementStatus.PENDING
            booking.settlement_payout_id = None

        amount_paise = to_paise(sum((self._owed_amount(b) for b in stuck), Decimal("0")))
        self.db.add(
            PayoutAudit(
                admin_user_id=admin_user_id,
                provider_id=provider_id,
                source="admin",
                amount_paise=amount_paise,
                booking_count=len(stuck),
                payout_id=None,
                outcome="requeued",
            )
        )
        await self.db.flush()

        logger.info(
            "payout.requeue admin=%s provider=%s from=%s bookings=%s",
            admin_user_id,
            provider_id,
            from_status.value,
            len(stuck),
        )
        return {
            "requeued": len(stuck),
            "from_status": from_status.value,
            "provider_id": str(provider_id),
        }

    async def handle_payout_webhook(self, event: str, payload: dict) -> None:
        """
        Move a payout's settlements based on its outcome.

        - ``payout.processed`` → COMPLETED
        - ``payout.failed``    → FAILED (money may have moved → manual re-queue)
        - ``payout.reversed``  → REVERSED (completed money clawed back; also alerts)
        - ``payout.rejected``  → back to PENDING (RazorpayX refused before sending, so
          no money moved — safe to auto-retry; the reason is recorded)
        - ``payout.queued``    → no state change (RazorpayX accepted, not yet sent)

        Bookings are matched on the RazorpayX payout id (``settlement_payout_id``), so
        concurrent payouts settle independently. Idempotent: only bookings in a
        transitionable state for that payout are touched.
        """
        handled = {
            "payout.processed",
            "payout.failed",
            "payout.reversed",
            "payout.rejected",
            "payout.queued",
        }
        if event not in handled:
            # payout.updated, transaction.*, etc. are a legitimate no-op.
            logger.info("Ignoring unhandled RazorpayX webhook event: %s", event)
            return

        entity = payload.get("payout", {}).get("entity", {})
        payout_id = entity.get("id")
        if not payout_id:
            # A handled event with no payout id is malformed — we can't map it to any
            # settlement. Surface it instead of failing silently.
            logger.error("RazorpayX %s webhook missing payout.entity.id", event)
            raise PaymentError("Payout webhook missing payout id")

        # queued: RazorpayX accepted the payout but hasn't sent it yet — no state
        # change. The create path already recorded settlement_payout_id in the normal
        # case; the rare dual-write orphan (id not yet linked) is re-linked by
        # reconcile_payouts — which cross-checks the amount before matching — rather
        # than here, so all payout→booking re-linking lives in one guarded place.
        if event == "payout.queued":
            linked = (
                (
                    await self.db.execute(
                        select(Booking).where(Booking.settlement_payout_id == payout_id)
                    )
                )
                .scalars()
                .all()
            )
            logger.info(
                "payout.webhook event=queued payout_id=%s linked=%s (no state change)",
                payout_id,
                len(linked),
            )
            return

        # rejected: RazorpayX refused before sending — no money left the account, so
        # return the bookings to PENDING (the next payout run re-picks them), clear the
        # payout id, and record the reason. Contrast with failed, which stays FAILED
        # and requires a manual admin re-queue.
        if event == "payout.rejected":
            reason = self._failure_reason(entity)
            rejected = (
                (
                    await self.db.execute(
                        select(Booking).where(
                            Booking.settlement_payout_id == payout_id,
                            Booking.settlement_status == SettlementStatus.PROCESSING,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for booking in rejected:
                booking.settlement_status = SettlementStatus.PENDING
                booking.settlement_payout_id = None
                booking.settlement_failure_reason = reason
            await self.db.flush()
            logger.info(
                "payout.webhook event=rejected payout_id=%s returned_to_pending=%s reason=%s",
                payout_id,
                len(rejected),
                reason,
            )
            return

        # processed / failed / reversed — terminal transitions.
        terminal = {
            "payout.processed": SettlementStatus.COMPLETED,
            "payout.failed": SettlementStatus.FAILED,
            "payout.reversed": SettlementStatus.REVERSED,
        }
        new_status = terminal[event]
        # A failed payout carries a reason (rejected/reversed do not, per RazorpayX);
        # record it so an admin can see why before deciding to re-queue the FAILED
        # settlement.
        failure_reason = self._failure_reason(entity) if event == "payout.failed" else None
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
            if event == "payout.failed":
                booking.settlement_failure_reason = failure_reason
        await self.db.flush()

        logger.info(
            "payout.webhook event=%s payout_id=%s settled=%s -> %s",
            event,
            payout_id,
            len(settled),
            new_status.value,
        )
        # Notify only when this webhook actually moved something: a redelivery of an
        # already-applied event settles nothing, and must not re-push.
        #
        # Buffered rather than enqueued: if this transaction rolls back, RazorpayX
        # retries the webhook and the second attempt settles the same bookings again
        # — an inline push would have already fired on the first attempt, so the
        # provider would be told twice. The in-memory ``settled`` filter above only
        # protects redelivery after a *successful* commit.
        if settled:
            self._buffer_notification(
                settled[0].provider_id,
                new_status.value,
                self._batch_amount_paise(entity, list(settled)),
                payout_id,
            )
        if event == "payout.reversed":
            # Completed money was clawed back — always surface for a human to review.
            logger.error(
                "ALERT payout.reversed payout_id=%s reversed_bookings=%s",
                payout_id,
                len(settled),
            )

    @staticmethod
    def _failure_reason(entity: dict) -> str | None:
        """Best-effort failure/rejection reason from a RazorpayX payout entity.

        The webhook payload carries ``failure_reason`` (string) and an ``error`` object
        (``description``/``source``/``reason``). Razorpay is migrating this detail to a
        newer ``status_details`` object (deprecating ``failure_reason``/``error``), so we
        check that first for forward-compat, then fall back to the current fields.

        Note: RazorpayX does not populate a reason for ``payout.rejected`` (it's null);
        the meaningful string appears on ``payout.failed``.
        """
        details = entity.get("status_details") or {}
        error = entity.get("error") or {}
        reason = (
            details.get("description")
            or details.get("reason")
            or entity.get("failure_reason")
            or error.get("description")
            or error.get("reason")
        )
        return reason[:500] if reason else None

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

        Provider pushes for these repairs are **not** enqueued here — they're returned
        under ``notifications`` for the caller to dispatch *after* it commits. One run
        repairs many payouts in a single transaction, so a failure part-way through
        rolls all of them back; a push already sent for an earlier repair could not be
        taken back, and would tell a provider their money arrived when the repair never
        landed. Pass the list to ``dispatch_notifications`` once the commit succeeds.
        """
        payouts = await list_payouts(from_ts=since_ts, to_ts=until_ts)
        report = {"checked": 0, "case_a": 0, "case_b": 0, "unresolved": 0}
        self._pending_notifications = []

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
                    payout.get("id"),
                    payout.get("reference_id"),
                    payout.get("status"),
                )

        await self.db.flush()
        logger.info("payout.reconcile %s", report)
        report["notifications"] = self._pending_notifications
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
                (
                    await session.execute(
                        select(PayoutAudit.id).where(PayoutAudit.reference_id == reference_id)
                    )
                )
                .scalars()
                .first()
            )
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
                    result.rowcount,
                    audit_id,
                )

    @staticmethod
    def _notify_payout(
        provider_id: UUID,
        event: str,
        amount_paise: int,
        payout_id: str | None,
    ) -> None:
        """Enqueue a provider push for a payout state change.

        One push per payout (not per booking) — the task is keyed on the provider and
        carries the batch total. Enqueued rather than sent inline so a slow FCM call
        never blocks a payout or webhook request.

        A broker failure must never roll back a settlement transition — the money has
        already moved, and losing that state to a missing push would be far worse than
        a missing push. So the enqueue is best-effort: failures are logged, not raised.
        """
        from app.tasks.notification_tasks import send_payout_notification

        try:
            send_payout_notification.delay(
                provider_id=str(provider_id),
                event=event,
                amount_paise=amount_paise,
                payout_id=payout_id,
            )
        except Exception as exc:
            logger.error(
                "payout.notify enqueue failed provider=%s event=%s payout_id=%s error=%s",
                provider_id,
                event,
                payout_id,
                exc,
            )

    def _buffer_notification(
        self,
        provider_id: UUID,
        event: str,
        amount_paise: int,
        payout_id: str | None,
    ) -> None:
        """Hold a push until the transaction that justifies it has committed.

        Every path buffers, because a push cannot be un-sent and none of these
        transactions are committed at the point the state changes:

        - **create** — RazorpayX has already taken the money by the time we write
          PROCESSING. If the commit then fails, an inline push would have announced
          a payout the database has no record of.
        - **webhook** — a rolled-back delivery is retried by RazorpayX, and the
          retry settles the same bookings again; an inline push would fire twice.
        - **reconcile** — one transaction repairs many payouts, so a failure
          part-way rolls back repairs whose pushes already went out.

        Drained by ``dispatch_pending_notifications`` (request paths) or
        ``dispatch_notifications`` (the reconcile task) — both only after commit.
        """
        self._pending_notifications.append(
            {
                "provider_id": str(provider_id),
                "event": event,
                "amount_paise": amount_paise,
                "payout_id": payout_id,
            }
        )

    def dispatch_pending_notifications(self) -> None:
        """Drain this instance's buffered pushes. **Must run after the commit.**

        Request paths hand this to FastAPI ``BackgroundTasks``, which runs it after
        the ``get_db`` dependency has committed — and not at all if that commit
        raises, which is precisely the property we want.
        """
        notifications, self._pending_notifications = self._pending_notifications, []
        self.dispatch_notifications(notifications)

    @classmethod
    def dispatch_notifications(cls, notifications: list[dict]) -> None:
        """Enqueue buffered pushes. Call only after the state change is committed."""
        for note in notifications:
            cls._notify_payout(**note)

    @staticmethod
    def _batch_amount_paise(payout: dict, bookings: list[Booking]) -> int:
        """Payout amount in paise, falling back to the batch's own owed total.

        Uses ``_owed_amount`` so tips are included in the fallback estimate
        (SVC-39 G23) — matches what ``create_provider_payout`` charges.
        """
        return payout.get("amount") or to_paise(
            sum((PayoutService._owed_amount(b) for b in bookings), Decimal("0"))
        )

    @staticmethod
    def _batch_idempotency_key(bookings: list[Booking]) -> str:
        """Stable key for a payout batch — a hash of its sorted booking ids."""
        ids = ",".join(sorted(str(b.id) for b in bookings))
        return "payout_" + hashlib.sha256(ids.encode()).hexdigest()[:32]

    async def _reconcile_linked(self, payout: dict, target: SettlementStatus) -> int | None:
        """Case A: repair bookings already linked to this payout id.

        Transitions linked bookings not yet in ``target`` and returns how many were
        repaired (0 if already consistent). Returns ``None`` when no booking
        references this payout id — i.e. it's an orphan for Case B to handle.
        """
        payout_id = payout.get("id")
        linked = (
            (
                await self.db.execute(
                    select(Booking).where(Booking.settlement_payout_id == payout_id)
                )
            )
            .scalars()
            .all()
        )
        if not linked:
            return None

        repaired = [b for b in linked if b.settlement_status != target]
        for booking in repaired:
            booking.settlement_status = target
        if repaired:
            self._add_reconciliation_audit(
                provider_id=repaired[0].provider_id,
                payout=payout,
                target=target,
                count=len(repaired),
            )
            # Reconciliation exists because the webhook never arrived — so neither did
            # the provider's push. Buffer it (not enqueue) so it only goes out once the
            # repair is committed; only repaired rows qualify, so a run over
            # already-consistent payouts stays silent.
            self._buffer_notification(
                repaired[0].provider_id,
                target.value,
                self._batch_amount_paise(payout, repaired),
                payout.get("id"),
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
            (
                await self.db.execute(
                    select(PayoutAudit).where(PayoutAudit.reference_id == reference_id)
                )
            )
            .scalars()
            .first()
        )
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
            provider_id=audit.provider_id,
            payout=payout,
            target=target,
            count=len(pending),
        )
        # The create path crashed before linking the payout, so no push went out for
        # the initiation either — the terminal state is the provider's first news.
        self._buffer_notification(
            audit.provider_id,
            target.value,
            self._batch_amount_paise(payout, pending),
            payout_id,
        )
        return len(pending)

    def _add_reconciliation_audit(
        self, *, provider_id: UUID, payout: dict, target: SettlementStatus, count: int
    ) -> None:
        """Record an automated (non-admin) reconciliation repair in payout_audits."""
        self.db.add(
            PayoutAudit(
                admin_user_id=None,
                provider_id=provider_id,
                source="reconciliation",
                amount_paise=payout.get("amount") or 0,
                booking_count=count,
                payout_id=payout.get("id"),
                reference_id=payout.get("reference_id"),
                outcome=target.value,
            )
        )

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
        """Return (vpa, account_number, ifsc) for the provider's **active** method.

        Returns exactly one destination. A provider may have both a UPI handle and a
        bank account stored — saving one never erases the other (SVC-148) — and
        ``create_payout`` sends to whichever it is given, preferring UPI if handed
        both. So this must resolve the choice rather than pass both along: a provider
        who switched from UPI to bank would otherwise keep being paid to the UPI
        handle they had just replaced.

        ``payout_method`` is the only authority on that. Column presence is not — it
        answers "what is stored", not "where should this money go".
        """
        if profile.payout_method is PayoutMethod.UPI:
            vpa = decrypt(profile.payout_vpa_encrypted) if profile.payout_vpa_encrypted else None
            if not vpa:
                raise PaymentError("Provider has no payout details on file")
            return vpa, None, None

        if profile.payout_method is PayoutMethod.BANK:
            account_number = (
                decrypt(profile.payout_account_number_encrypted)
                if profile.payout_account_number_encrypted
                else None
            )
            ifsc = decrypt(profile.payout_ifsc_encrypted) if profile.payout_ifsc_encrypted else None
            if not (account_number and ifsc):
                raise PaymentError("Provider has no payout details on file")
            return None, account_number, ifsc

        raise PaymentError("Provider has no payout details on file")

    @staticmethod
    def _owed_amount(booking: Booking) -> Decimal:
        """How much a settled booking is worth to its provider — the single
        definition (SVC-39 G23).

        Provider payout is the 80% commission split; ``tip_amount`` is 100%
        provider (see ``Booking.tip_amount``). Both are ``Numeric`` post-G31,
        so the sum is exact.

        Shared across every payout-money site (create, requeue, reconcile,
        admin list) so a change to what counts as "owed" doesn't require
        edits in four places, and the amount the admin approves is the
        amount that leaves the bank.
        """
        return booking.provider_payout + booking.tip_amount

    @staticmethod
    def _owed_filter() -> tuple:
        """What counts as "money owed to a provider" — the single definition.

        Deliberately shared by the payout path (``_pending_settlements``) and the
        admin list (``list_pending_settlements``). If those two ever drift apart the
        admin sees one amount in the confirmation dialog and a different amount
        leaves the bank, so the condition lives in exactly one place.

        ``payment_status == PAID`` was added for SVC-39 G1: before it, "owed"
        meant only "job is done and we haven't paid yet", so the platform paid
        providers for bookings the consumer never actually paid for (an
        abandoned checkout, a failed payment, a webhook that never arrived).
        Depends on SVC-39 G9 landing first — before G9, online bookings had
        their PAID stamp wiped on COMPLETED and this filter would match nothing
        for them.

        ``payment_method != CASH`` was added for SVC-39 G2: on a cash job the
        provider has already collected the full amount in hand.
        ``process_cash_payment`` flips the booking to PAID once completed, so
        after G1 the row satisfies status+PAID+PENDING and would be picked up
        by the next payout batch — the platform would send the provider their
        80% again via RazorpayX, on top of the cash they are already holding.
        (The 20% platform commission on cash is a receivable we owe ourselves;
        it is not modelled anywhere yet — separate follow-up.)
        """
        return (
            Booking.status == BookingStatus.COMPLETED,
            Booking.payment_status == PaymentStatus.PAID,
            Booking.payment_method != PaymentMethod.CASH,
            Booking.settlement_status == SettlementStatus.PENDING,
        )

    async def _pending_settlements(self, provider_id: UUID) -> list[Booking]:
        # Lock the selected rows for the duration of the transaction so two
        # concurrent payouts for the same provider can't both read the same
        # PENDING bookings and issue divergent batches (→ two payouts, double-pay).
        # skip_locked: a second concurrent click doesn't block — it skips the
        # locked rows, finds nothing PENDING, and raises "No pending settlements".
        result = await self.db.execute(
            select(Booking)
            .where(Booking.provider_id == provider_id, *self._owed_filter())
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())
