"""
Payment service: Razorpay order creation, signature verification, and webhook handling.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import BookingNotFound, PaymentError, UnauthorizedAction
from app.models.booking import Booking, PaymentStatus
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.utils.money import to_paise
from app.utils.razorpay_client import razorpay_client

settings = get_settings()
logger = logging.getLogger(__name__)


def _capture_dispute_alert(
    *,
    event: str,
    payment_id: str,
    booking_id: UUID,
    dispute_entity: dict,
) -> None:
    """Page an admin via Sentry for a dispute we care about (SVC-187 / G15).

    Guarded on ``sentry_sdk`` being importable — SENTRY_DSN is unset in dev,
    matching the pattern in ``dependencies.py`` and ``middleware/request_id.py``.
    Fingerprint keys on ``(event, dispute_id)`` so Razorpay webhook redeliveries
    collapse into one Sentry issue per real dispute event; tags carry the ids
    triage needs without opening the payload.
    """
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover — sentry_sdk is a runtime dep in prod
        return

    dispute_id = dispute_entity.get("id")
    # `new_scope` is the sentry-sdk 2.x replacement for the deprecated
    # `push_scope`; both push a scope that pops on exit, but new_scope is
    # the one that survives the eventual 3.x removal.
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("payment_id", payment_id)
        scope.set_tag("booking_id", str(booking_id))
        scope.set_tag("dispute_id", dispute_id or "unknown")
        scope.set_tag("dispute_event", event)
        if reason := dispute_entity.get("reason_code"):
            scope.set_tag("dispute_reason_code", reason)
        # Fingerprint on (event, dispute_id) — redeliveries of the same event
        # for the same dispute collapse into one alert; distinct dispute
        # events for the same payment stay separate.
        scope.fingerprint = [event, dispute_id or payment_id]
        sentry_sdk.capture_message(
            f"{event} payment={payment_id} dispute={dispute_id}",
            level="error",
        )


class PaymentService:
    """Manages Razorpay order lifecycle and payment verification."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Order creation
    # ------------------------------------------------------------------

    async def create_order(self, booking_id: UUID, consumer_id: UUID) -> dict:
        """
        Create a Razorpay order for the booking amount.

        For beta the payment lands fully in the platform settlement account — no
        Route ``transfers`` split, so a provider does not need a linked account.
        Provider payouts are handled separately via RazorpayX. Returns order
        details for frontend checkout.

        SVC-39 G22: charges ``service_amount − discount_amount``. The coupon
        module docstring documents the rule (``effective charge = service_amount
        − D + tip_amount``); tips are added post-completion via ``set_tip``, so
        at order-creation time only the discount applies. Before this fix, every
        coupon booking billed the consumer the full pre-discount amount while
        the coupon was recorded as redeemed.

        SVC-39 G5: refuses when ``consumer_id`` doesn't own the booking. The
        route used to take ``current_user`` only for authentication and pass it
        nowhere, so any logged-in user could mint an order for any booking id —
        reading back the amount, and (worse) overwriting the target booking's
        ``razorpay_order_id`` so the legitimate consumer paid against an order
        the booking no longer referenced. The ownership check lives in the
        service, not the router, because the same invariant applies to every
        caller (existing router today, future admin/backfill scripts, tests).
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)
        if booking.consumer_id != consumer_id:
            # UnauthorizedAction maps to 403 — this is a "you should never have
            # been able to do this" case, distinct from a 404 hiding existence.
            raise UnauthorizedAction("You do not have access to this booking")

        # Numeric arithmetic (both operands are Decimal post-SVC-150 + G31), so
        # the subtraction is exact and ``to_paise`` rounds half-up — a ₹100.09
        # booking with a ₹10 coupon bills 9009 paise, not 9008.
        chargeable = booking.service_amount - booking.discount_amount
        amount_paise = to_paise(chargeable)

        order_data = {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": str(booking.id),
            # SVC-174 (G7): partial_payment=0 shuts the door on Razorpay accepting
            # a short payment for this order. Coupled with the amount check in
            # verify_payment, this makes "amount mismatch" impossible-then-caught
            # rather than caught-only.
            "partial_payment": 0,
            # SVC-190 (G14): the checkout window. Razorpay refuses payment
            # after `expire_by`, so a client that cached this order id and
            # tries to submit it days later gets a hard refusal at the wire
            # rather than reaching our verify path against an EXPIRED
            # booking. The local sweep (`expire_abandoned_payments`) flips
            # `payment_status = EXPIRED` after this + grace so the two views
            # of the world agree. `expire_by` is a Unix second, per Razorpay's
            # spec.
            "expire_by": int(time.time()) + settings.PAYMENT_ORDER_EXPIRY_SECONDS,
        }

        order = razorpay_client.order.create(data=order_data)
        booking.razorpay_order_id = order["id"]
        await self.db.flush()

        return {
            "razorpay_order_id": order["id"],
            "amount": amount_paise,
            "currency": "INR",
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        }

    # ------------------------------------------------------------------
    # Payment verification
    # ------------------------------------------------------------------

    async def verify_payment(
        self,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
        consumer_id: UUID,
    ) -> Payment:
        """
        Verify the Razorpay HMAC-SHA256 signature.
        On success, create a Payment record and mark the booking as paid.

        SVC-173 (G5): also refuses if ``consumer_id`` doesn't own the booking
        the order was created for. The signature check alone would let a caller
        flip *someone else's* booking to PAID by relaying the victim's order
        id + a valid signature (from a completed Razorpay session they saw).

        SVC-174 (G7): the signature only proves the (order, payment) pair was
        signed by our key. It says nothing about the amount, the currency, or
        whether the payment was actually captured — a partial payment, a
        currency mismatch, or an authorized-but-not-captured payment would all
        have flipped the booking to PAID. Now: fetch the payment entity from
        Razorpay after signature verification and assert
        ``status == "captured"``, ``currency == "INR"``, and
        ``amount == to_paise(booking.service_amount - booking.discount_amount)``
        (matches SVC-39 G22 charging). Fails 402 on any mismatch — the row is
        not written.

        SVC-175 (G6): idempotent on ``razorpay_payment_id``. The signature is a
        pure function of ``order_id|payment_id`` and stays valid forever, so
        every re-submission of the same request used to write another CAPTURED
        row. Now: query first, insert only if absent. The partial unique index
        on ``razorpay_payment_id`` (migration ``5b293fc63525``) is the tie-breaker
        against a concurrent double-call — the second caller's insert fails on
        the constraint and we re-query the winner.

        SVC-179 (G11): reads ``method`` from the fetched entity instead of the
        hard-coded UPI. Card / netbanking / UPI now get their real values on
        the ledger row.
        """
        expected = hmac.new(
            key=settings.RAZORPAY_KEY_SECRET.encode(),
            msg=f"{razorpay_order_id}|{razorpay_payment_id}".encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, razorpay_signature):
            raise PaymentError("Payment signature verification failed")

        booking = await self._find_booking_by_order(razorpay_order_id)
        if booking.consumer_id != consumer_id:
            raise UnauthorizedAction("You do not have access to this booking")

        existing = await self._find_payment_by_razorpay_id(razorpay_payment_id)
        if existing is not None:
            # A prior verify already recorded this payment. Belt-and-braces: make
            # sure the booking is flagged PAID (a lost response before the flip
            # would otherwise strand the row on PENDING); no-op if it already is.
            if booking.payment_status is not PaymentStatus.PAID:
                booking.payment_status = PaymentStatus.PAID
                await self.db.flush()
            return existing

        # SVC-174 (G7): confirm the payment entity BEFORE we write the ledger row.
        # razorpay_client is sync; wrap in a thread so we don't block the loop.
        entity = await asyncio.to_thread(razorpay_client.payment.fetch, razorpay_payment_id)
        expected_paise = to_paise(booking.service_amount - booking.discount_amount)
        self._assert_payment_matches(entity, expected_paise)

        payment = Payment(
            booking_id=booking.id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_order_id=razorpay_order_id,
            amount=booking.service_amount,
            platform_fee=booking.platform_commission,
            provider_settlement=booking.provider_payout,
            status=PaymentRecordStatus.CAPTURED,
            # SVC-179 (G11): real method from the fetched entity.
            method=self._resolve_method(entity.get("method")),
        )
        self.db.add(payment)

        booking.payment_status = PaymentStatus.PAID
        try:
            await self.db.flush()
        except IntegrityError:
            # A concurrent verify raced us and inserted first — the partial
            # unique index refused our duplicate. Roll back to a savepoint via
            # session.rollback() would blow away our booking edit above; safer
            # to re-query the winner and return it. The booking's payment_status
            # is idempotent (PAID either way), so nothing else needs undoing.
            await self.db.rollback()
            winner = await self._find_payment_by_razorpay_id(razorpay_payment_id)
            if winner is None:  # pragma: no cover - defensive; index guarantees a winner exists
                raise PaymentError("Payment write raced but no winning row was found") from None
            # Re-flag PAID after rollback (rollback undid our earlier flip).
            booking = await self._find_booking_by_order(razorpay_order_id)
            if booking.payment_status is not PaymentStatus.PAID:
                booking.payment_status = PaymentStatus.PAID
                await self.db.flush()
            return winner

        return payment

    # ------------------------------------------------------------------
    # Webhook handler
    # ------------------------------------------------------------------

    async def handle_webhook(self, event: str, payload: dict) -> None:
        """
        Handle Razorpay webhook events (idempotent).
        Primary events: payment.captured, payment.failed, payment.dispute.*.

        SVC-176 (G8): the ``payment.captured`` branch now writes/upserts the
        same ledger row ``verify_payment`` does. Before this, a consumer who
        killed the app after paying had their booking flipped to PAID with
        **no ledger row at all** — no amount, no payment id, no method — and
        a chargeback on such a payment could never be matched (the dispute
        handler keys on ``razorpay_payment_id``).

        Also SVC-176: unknown ``order_id`` on ``payment.captured`` used to
        raise ``PaymentError`` (→ 402), which Razorpay reads as a delivery
        failure and re-sends on its full retry schedule *forever*, even
        though no retry can ever succeed. Matches the dispute handler's
        policy — log and ack (200) — instead.
        """
        if event.startswith("payment.dispute."):
            await self._handle_dispute(event, payload)
            return

        if event == "payment.captured":
            payment_entity = payload.get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            payment_id = payment_entity.get("id")
            if not order_id:
                logger.warning("payment.captured webhook missing order_id in payload")
                return

            booking = await self._find_booking_by_order_or_none(order_id)
            if booking is None:
                # SVC-176: an unknown order_id can never be resolved by retry.
                # Ack and log (matches the dispute handler's policy) so
                # Razorpay stops the retry storm.
                logger.warning(
                    "payment.captured for unknown order_id=%s payment_id=%s — acking",
                    order_id,
                    payment_id,
                )
                return

            # SVC-176: upsert a CAPTURED ledger row so the ledger survives
            # even when verify_payment never ran (consumer killed the app
            # after paying). Same idempotency shape as verify_payment.
            if payment_id:
                await self._upsert_captured_ledger_row(booking, payment_entity)

            if booking.payment_status != PaymentStatus.PAID:
                booking.payment_status = PaymentStatus.PAID
                await self.db.flush()

        elif event == "payment.failed":
            payment_entity = payload.get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            payment_id = payment_entity.get("id")
            if not order_id:
                logger.warning("payment.failed webhook missing order_id in payload")
                return

            booking = await self._find_booking_by_order_or_none(order_id)
            if booking is None:
                logger.warning(
                    "payment.failed for unknown order_id=%s payment_id=%s — acking",
                    order_id,
                    payment_id,
                )
                return

            try:
                # Create a failed payment record for audit trail.
                payment = Payment(
                    booking_id=booking.id,
                    razorpay_order_id=order_id,
                    razorpay_payment_id=payment_id or None,
                    amount=booking.service_amount,
                    platform_fee=booking.platform_commission,
                    provider_settlement=booking.provider_payout,
                    status=PaymentRecordStatus.FAILED,
                    # SVC-179 (G11): real method from the webhook payload.
                    method=self._resolve_method(payment_entity.get("method")),
                )
                self.db.add(payment)
                await self.db.flush()
            except Exception:
                # SVC-176: was silently swallowed. Log so a broken FAILED-audit
                # write is diagnosable — the failure is genuinely non-fatal
                # (the payout path doesn't read from `payments`), but we
                # shouldn't lose the signal.
                logger.exception(
                    "payment.failed ledger write raised for order_id=%s payment_id=%s",
                    order_id,
                    payment_id,
                )

    # ------------------------------------------------------------------
    # Disputes / chargebacks
    # ------------------------------------------------------------------

    async def _handle_dispute(self, event: str, payload: dict) -> None:
        """
        Record a Razorpay chargeback against its Payment.

        ``payment.dispute.created`` fires when a customer disputes a payment with
        their card issuer; ``.won`` / ``.lost`` follow when it resolves. For beta we
        only stamp ``disputed_at`` and alert — the refund and settlement reversal are
        decided by an admin out of band, so no payment or settlement status is moved
        here (SVC-134).

        Idempotent: Razorpay retries webhooks, and ``disputed_at`` is stamped once so
        a redelivery keeps the original dispute time.
        """
        # Dispute payloads are keyed by payment, not order: the id lives at
        # payload.dispute.entity.payment_id (payload.payment.entity.id carries the
        # same value). The captured/failed branches above match on order_id instead.
        dispute_entity = payload.get("dispute", {}).get("entity", {})
        payment_id = dispute_entity.get("payment_id")
        if not payment_id:
            # Can't map the dispute to anything — surface it rather than 200-ing.
            logger.error("Razorpay %s webhook missing dispute.entity.payment_id", event)
            raise PaymentError("Dispute webhook missing payment id")

        payment = await self._find_payment_by_razorpay_id(payment_id)
        if not payment:
            # A dispute for a payment we have no ledger row for is an anomaly worth
            # investigating, but a retry can't fix it — log loudly and acknowledge.
            logger.error(
                "Razorpay %s for unknown payment %s (dispute=%s)",
                event,
                payment_id,
                dispute_entity.get("id"),
            )
            return

        if event == "payment.dispute.created":
            if payment.disputed_at is None:
                payment.disputed_at = datetime.now(UTC)
                await self.db.flush()
            logger.warning(
                "payment.dispute.created payment=%s booking=%s dispute=%s "
                "amount=%s reason=%s respond_by=%s",
                payment_id,
                payment.booking_id,
                dispute_entity.get("id"),
                dispute_entity.get("amount"),
                dispute_entity.get("reason_code"),
                dispute_entity.get("respond_by"),
            )
            # SVC-187 (G15): page an admin. Sentry event with tags so the alert
            # rule can filter and the message so triage sees the identifiers
            # without opening the payload. Fingerprint groups by event kind so
            # a redelivery collapses into one alert per (kind, dispute).
            _capture_dispute_alert(
                event=event,
                payment_id=payment_id,
                booking_id=payment.booking_id,
                dispute_entity=dispute_entity,
            )

        elif event == "payment.dispute.lost":
            logger.warning(
                "%s payment=%s booking=%s dispute=%s (no automated state change)",
                event,
                payment_id,
                payment.booking_id,
                dispute_entity.get("id"),
            )
            # SVC-187 (G15): lost means money went out and an admin needs to
            # decide on a clawback out of band — paged same as .created.
            _capture_dispute_alert(
                event=event,
                payment_id=payment_id,
                booking_id=payment.booking_id,
                dispute_entity=dispute_entity,
            )
            # SVC-193: write the clawback debit on the provider's wallet.
            # Amount is the provider's share of the disputed booking
            # (`provider_payout + tip_amount`), not the whole payment — a
            # dispute reverses the customer charge, but the platform's cut
            # was ours to lose, and the tip was optional consumer generosity
            # that vanishes with the reversal. Wallet is likely to go
            # negative (provider was already paid out); the next credit or
            # an admin correction clears it.
            booking = await self.db.get(Booking, payment.booking_id)
            if booking is not None and booking.provider_id is not None:
                from app.services.ledger_service import debit_dispute_loss
                from app.utils.money import to_paise

                clawback_paise = to_paise(booking.provider_payout + booking.tip_amount)
                if clawback_paise > 0:
                    await debit_dispute_loss(
                        self.db,
                        provider_id=booking.provider_id,
                        payment_id=payment.id,
                        amount_paise=clawback_paise,
                        note=(
                            f"dispute.lost payment={payment_id} "
                            f"dispute={dispute_entity.get('id')}"
                        ),
                    )

        elif event == "payment.dispute.won":
            # No money at risk — log only, do not page.
            logger.warning(
                "%s payment=%s booking=%s dispute=%s (no automated state change)",
                event,
                payment_id,
                payment.booking_id,
                dispute_entity.get("id"),
            )

        else:
            # e.g. payment.dispute.under_review / .action_required / .closed
            logger.info("Ignoring unhandled dispute event: %s", event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _find_payment_by_razorpay_id(self, payment_id: str) -> Payment | None:
        """Look up a ledger row by Razorpay payment id (disputes key on this)."""
        result = await self.db.execute(
            select(Payment)
            .where(Payment.razorpay_payment_id == payment_id)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_booking_by_order(self, order_id: str) -> Booking:
        result = await self.db.execute(select(Booking).where(Booking.razorpay_order_id == order_id))
        booking = result.scalar_one_or_none()
        if not booking:
            raise PaymentError(f"No booking found for order {order_id}")
        return booking

    async def _find_booking_by_order_or_none(self, order_id: str) -> Booking | None:
        """SVC-176 (G8): non-raising sibling of ``_find_booking_by_order``.

        The webhook handlers want to log-and-ack an unknown order rather than
        raise — otherwise Razorpay retries a delivery that can never succeed.
        `verify_payment` still uses the raising version (a stranger's
        payment_id there is a real 4xx to surface to the caller).
        """
        result = await self.db.execute(select(Booking).where(Booking.razorpay_order_id == order_id))
        return result.scalar_one_or_none()

    async def _upsert_captured_ledger_row(self, booking: Booking, payment_entity: dict) -> None:
        """SVC-176 (G8): write/upsert a CAPTURED ledger row for a webhook-driven
        successful payment. Same idempotency shape as `verify_payment`.

        Called only from the `payment.captured` webhook branch — the verify
        path already writes the row, and calling this on top would be a
        second lookup-and-noop. Bytes-identical to the verify write for the
        fields we know here; the entity's `method` beats a hard-coded UPI
        (SVC-179 G11).
        """
        payment_id = payment_entity.get("id")
        if not payment_id:
            return
        existing = await self._find_payment_by_razorpay_id(payment_id)
        if existing is not None:
            return

        row = Payment(
            booking_id=booking.id,
            razorpay_payment_id=payment_id,
            razorpay_order_id=payment_entity.get("order_id"),
            amount=booking.service_amount,
            platform_fee=booking.platform_commission,
            provider_settlement=booking.provider_payout,
            status=PaymentRecordStatus.CAPTURED,
            method=self._resolve_method(payment_entity.get("method")),
        )
        self.db.add(row)
        try:
            await self.db.flush()
        except IntegrityError:
            # Concurrent webhook redelivery raced us. Rollback and re-query —
            # same shape as verify_payment. The booking-flip that follows in
            # handle_webhook is idempotent, so it re-applies safely.
            await self.db.rollback()

    @staticmethod
    def _assert_payment_matches(entity: dict, expected_paise: int) -> None:
        """SVC-174 (G7): raise PaymentError if the fetched entity doesn't match what
        the booking expects. The signature can't detect any of these on its own.

        Every mismatch is a 402 (``PaymentError``) with the specific reason in the
        message — deliberately not one merged "invalid payment" error so the admin
        looking at logs knows whether Razorpay served a different currency, a
        smaller amount, or an authorized-but-not-captured payment.
        """
        entity_status = entity.get("status")
        if entity_status != "captured":
            raise PaymentError(
                f"Payment {entity.get('id')!r} status={entity_status!r}, expected 'captured'"
            )
        entity_currency = entity.get("currency")
        if entity_currency != "INR":
            raise PaymentError(
                f"Payment {entity.get('id')!r} currency={entity_currency!r}, expected 'INR'"
            )
        entity_amount = entity.get("amount")
        if entity_amount != expected_paise:
            raise PaymentError(
                f"Payment {entity.get('id')!r} amount={entity_amount} paise, "
                f"expected {expected_paise} paise (partial payment or amount tampering)"
            )

    @staticmethod
    def _resolve_method(razorpay_method: str | None) -> PaymentRecordMethod:
        """SVC-179 (G11): map Razorpay's method string to our enum.

        Razorpay uses lowercase ``upi`` / ``card`` / ``netbanking`` / ``wallet`` /
        ``emi`` / etc. Our enum covers UPI / CARD / NETBANKING / CASH — the rest
        collapse to UPI as a beta-safe fallback so the ledger row still writes;
        the mislabel is a reporting problem, not a money problem, and adding an
        ``OTHER`` enum member is a follow-up.
        """
        mapping = {
            "upi": PaymentRecordMethod.UPI,
            "card": PaymentRecordMethod.CARD,
            "netbanking": PaymentRecordMethod.NETBANKING,
        }
        resolved = mapping.get((razorpay_method or "").lower())
        if resolved is not None:
            return resolved
        logger.warning(
            "Razorpay method=%r not in {upi, card, netbanking} — recording as UPI",
            razorpay_method,
        )
        return PaymentRecordMethod.UPI
