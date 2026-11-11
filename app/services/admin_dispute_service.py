"""SVC-431: customer disputes and the refunds that settle them.

An admin logs a dispute on the customer's behalf (there is no consumer
raise-dispute endpoint yet), may pick it up (RAISED → IN_REVIEW), and resolves
it with a full, partial or no refund.

**What can be refunded.** ``captured`` is what the customer paid for the
service: ``service_amount − discount_amount`` (the tip is never refundable).
Online, that needs a Razorpay payment row (CAPTURED, or REFUNDED once a cancel
refund's webhook landed); cash, the CAPTURED cash row ``process_cash_payment``
writes on completion. The coupon model says a cash customer was billed
service − discount too, though that row records ``service_amount``.
``refundable = captured − cancel refund − earlier dispute refunds``, where the
cancel refund (``payments.refund_amount_paise``) counts whatever its state and a
FAILED Razorpay dispute refund doesn't count.

**Provider pay is untouched.** The platform absorbs every dispute refund: this
module writes ``booking_disputes`` and ``admin_actions`` only, never
``booking.payment_status``, ``payments.status``, ``payments.refund_*`` or the
ledger, so ``PayoutService._owed_filter`` sees the booking exactly as before.
A clawback is a later ticket.

**Money moves once.** Resolve locks the dispute, then the payment row, before it
calls Razorpay, so a double click waits and then finds the dispute RESOLVED. A
retry after a Razorpay timeout adopts the refund already made for this dispute
(``PaymentService.create_dispute_refund``).

**Statuses.** A Razorpay refund leaves the dispute RESOLVED with
``refund_status`` PENDING; ``refund.processed`` closes it, ``refund.failed``
marks it FAILED and leaves it RESOLVED (retry: SVC-213). A manual (cash) refund
and NO_REFUND have nothing pending, so they close at once.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.database import call_after_commit
from app.exceptions import AppException, BookingNotFound
from app.models.booking import Booking, BookingStatus, PaymentMethod
from app.models.booking_dispute import DISPUTE_OPEN_STATUSES, BookingDispute
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.pricing_config import (
    SUPPORT_REFUND_LIMIT_DEFAULT,
    SUPPORT_REFUND_LIMIT_KEY,
    PricingConfig,
)
from app.models.professional import Professional
from app.models.user import AdminRole, User
from app.schemas.admin_disputes import (
    AdminDisputeBookingContext,
    AdminDisputeCreate,
    AdminDisputeDetail,
    AdminDisputeListResponse,
    AdminDisputeParty,
    AdminDisputeRefunds,
    AdminDisputeResolve,
    AdminDisputeRow,
)
from app.services.admin_audit import AuditActor, audit_admin_action, snapshot
from app.utils.money import to_paise

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
# A dispute can only be logged once the job is over, so no cancel refund can
# follow (and overlap) a dispute refund.
DISPUTABLE_BOOKING_STATUSES = (BookingStatus.COMPLETED, BookingStatus.CANCELLED)

# The customer push sent after a resolve commits (notification_tasks).
PUSH_EVENT_FOR = {
    "RAZORPAY": "dispute_refunded",
    "MANUAL": "dispute_refunded_manual",
    None: "dispute_no_refund",
}


# -- errors -------------------------------------------------------------------


class DisputeNotFound(AppException):
    def __init__(self):
        super().__init__(
            "Dispute not found", status_code=status.HTTP_404_NOT_FOUND, code="DISPUTE_NOT_FOUND"
        )


class DisputeConflict(AppException):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(
            message, status_code=status.HTTP_409_CONFLICT, code=code, details=details or None
        )


class DisputeInvalid(AppException):
    def __init__(self, field: str, message: str, **extra: Any):
        super().__init__(
            message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="DISPUTE_INVALID",
            details={"field": field, **extra},
        )


class RefundOverSupportLimit(AppException):
    def __init__(self, limit: Decimal, amount: Decimal):
        super().__init__(
            f"Support can refund up to ₹{limit} on a dispute; ask an admin or ops for ₹{amount}",
            status_code=status.HTTP_403_FORBIDDEN,
            code="REFUND_OVER_SUPPORT_LIMIT",
            details={"limit": float(limit), "refund_amount": float(amount)},
        )


# -- money --------------------------------------------------------------------


@dataclass
class RefundContext:
    channel: str | None  # RAZORPAY, MANUAL, or None when nothing was paid
    payment: Payment | None
    captured: Decimal
    cancel_refund: Decimal
    dispute_refunds: Decimal

    @property
    def refundable(self) -> Decimal:
        return max(self.captured - self.cancel_refund - self.dispute_refunds, ZERO)


def _rupees(paise: int | None) -> Decimal:
    return (Decimal(paise or 0) / 100).quantize(CENT)


class AdminDisputeService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _refund_context(self, booking: Booking, *, lock: bool = False) -> RefundContext:
        if booking.payment_method is PaymentMethod.CASH:
            channel = "MANUAL"
            where = (
                Payment.status == PaymentRecordStatus.CAPTURED,
                Payment.method == PaymentRecordMethod.CASH,
            )
        else:
            channel = "RAZORPAY"
            where = (
                Payment.razorpay_payment_id.is_not(None),
                Payment.status.in_((PaymentRecordStatus.CAPTURED, PaymentRecordStatus.REFUNDED)),
            )
        stmt = (
            select(Payment)
            .where(Payment.booking_id == booking.id, *where)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        payment = await self.db.scalar(stmt)

        dispute_refunds = await self.db.scalar(
            select(func.coalesce(func.sum(BookingDispute.refund_amount), 0)).where(
                BookingDispute.booking_id == booking.id,
                BookingDispute.refund_amount.is_not(None),
                or_(
                    BookingDispute.refund_status.is_(None),
                    BookingDispute.refund_status != "FAILED",
                ),
            )
        )
        if payment is None:
            return RefundContext(None, None, ZERO, ZERO, Decimal(dispute_refunds))
        captured = max(booking.service_amount - booking.discount_amount, ZERO)
        cancel_refund = _rupees(payment.refund_amount_paise) if channel == "RAZORPAY" else ZERO
        return RefundContext(channel, payment, captured, cancel_refund, Decimal(dispute_refunds))

    async def _support_limit(self) -> Decimal:
        row = await self.db.get(PricingConfig, SUPPORT_REFUND_LIMIT_KEY)
        value = row.value_json if row is not None else SUPPORT_REFUND_LIMIT_DEFAULT
        try:
            return Decimal(str(value)).quantize(CENT)
        except Exception:
            # Fail closed: a bad stored value can't lift the cap.
            return Decimal(SUPPORT_REFUND_LIMIT_DEFAULT).quantize(CENT)

    # -- reads ----------------------------------------------------------------

    def _rows_stmt(self):
        customer = aliased(User)
        creator = aliased(User)
        resolver = aliased(User)
        return (
            select(
                BookingDispute,
                Booking.status,
                Booking.payment_method,
                customer.name,
                customer.phone,
                creator.name,
                resolver.name,
            )
            .select_from(BookingDispute)
            .join(Booking, Booking.id == BookingDispute.booking_id)
            .join(customer, customer.id == BookingDispute.raised_by_user_id)
            .outerjoin(creator, creator.id == BookingDispute.created_by)
            .outerjoin(resolver, resolver.id == BookingDispute.resolved_by)
        )

    @staticmethod
    def _row(d: BookingDispute, booking_status, payment_method, c_name, c_phone, by, res):
        return AdminDisputeRow(
            id=d.id,
            booking_id=d.booking_id,
            booking_status=booking_status,
            payment_method=payment_method,
            reason_code=d.reason_code,
            description=d.description,
            status=d.status,
            resolution_type=d.resolution_type,
            refund_amount=float(d.refund_amount) if d.refund_amount is not None else None,
            refund_channel=d.refund_channel,
            refund_status=d.refund_status,
            refund_failed_reason=d.refund_failed_reason,
            razorpay_refund_id=d.razorpay_refund_id,
            resolution_notes=d.resolution_notes,
            customer_id=d.raised_by_user_id,
            customer_name=c_name,
            customer_phone=c_phone,
            created_by=d.created_by,
            created_by_name=by,
            resolved_by=d.resolved_by,
            resolved_by_name=res,
            created_at=d.created_at,
            updated_at=d.updated_at,
            resolved_at=d.resolved_at,
            closed_at=d.closed_at,
        )

    async def _one_row(self, dispute_id: uuid.UUID) -> AdminDisputeRow:
        row = (
            await self.db.execute(self._rows_stmt().where(BookingDispute.id == dispute_id))
        ).one()
        return self._row(*row)

    async def list_disputes(
        self,
        *,
        status_filter: str | None,
        reason_code: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        page: int,
        per_page: int,
    ) -> AdminDisputeListResponse:
        """Newest first. ``created_at >= date_from`` and ``< date_to``."""
        where = []
        if status_filter is not None:
            where.append(BookingDispute.status == status_filter)
        if reason_code is not None:
            where.append(BookingDispute.reason_code == reason_code)
        if date_from is not None:
            where.append(BookingDispute.created_at >= _as_utc(date_from))
        if date_to is not None:
            where.append(BookingDispute.created_at < _as_utc(date_to))
        total = await self.db.scalar(select(func.count()).select_from(BookingDispute).where(*where))
        rows = await self.db.execute(
            self._rows_stmt()
            .where(*where)
            .order_by(BookingDispute.created_at.desc(), BookingDispute.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        return AdminDisputeListResponse(
            items=[self._row(*r) for r in rows.all()], total=total, page=page, per_page=per_page
        )

    async def detail(self, booking_id: uuid.UUID) -> AdminDisputeDetail:
        booking = await self.db.get(Booking, booking_id)
        if booking is None:
            raise BookingNotFound(booking_id)
        customer = await self.db.get(User, booking.consumer_id)
        provider = await self.db.get(User, booking.provider_id) if booking.provider_id else None
        pro = (
            await self.db.get(Professional, booking.professional_id)
            if booking.professional_id
            else None
        )
        ctx = await self._refund_context(booking)
        rows = await self.db.execute(
            self._rows_stmt()
            .where(BookingDispute.booking_id == booking_id)
            .order_by(BookingDispute.created_at.desc(), BookingDispute.id.desc())
        )
        disputes = [self._row(*r) for r in rows.all()]
        return AdminDisputeDetail(
            booking=AdminDisputeBookingContext(
                id=booking.id,
                status=booking.status,
                payment_method=booking.payment_method,
                payment_status=booking.payment_status,
                settlement_status=booking.settlement_status,
                service_amount=float(booking.service_amount),
                discount_amount=float(booking.discount_amount),
                tip_amount=float(booking.tip_amount),
                scheduled_at=booking.scheduled_at,
                created_at=booking.created_at,
                cancellation_reason=booking.cancellation_reason,
                customer=AdminDisputeParty(id=booking.consumer_id, name=customer.name),
                customer_phone=customer.phone,
                provider=AdminDisputeParty(id=provider.id, name=provider.name)
                if provider
                else None,
                professional=AdminDisputeParty(id=pro.id, name=pro.display_name) if pro else None,
            ),
            refunds=AdminDisputeRefunds(
                channel=ctx.channel,
                captured=float(ctx.captured),
                cancel_refund=float(ctx.cancel_refund),
                cancel_refund_id=ctx.payment.refund_id if ctx.payment else None,
                cancel_refunded_at=ctx.payment.refunded_at if ctx.payment else None,
                dispute_refunds=float(ctx.dispute_refunds),
                refundable=float(ctx.refundable),
                support_refund_limit=float(await self._support_limit()),
            ),
            open_dispute=next((d for d in disputes if d.status in DISPUTE_OPEN_STATUSES), None),
            disputes=disputes,
        )

    # -- writes ---------------------------------------------------------------

    async def create(self, actor: AuditActor, body: AdminDisputeCreate) -> AdminDisputeRow:
        booking = await self.db.get(Booking, body.booking_id)
        if booking is None:
            raise BookingNotFound(body.booking_id)
        if booking.status not in DISPUTABLE_BOOKING_STATUSES:
            raise DisputeConflict(
                "DISPUTE_BOOKING_NOT_FINISHED",
                "A dispute can only be logged on a completed or cancelled booking",
                booking_status=booking.status.value,
            )
        open_id = await self.db.scalar(
            select(BookingDispute.id).where(
                BookingDispute.booking_id == booking.id,
                BookingDispute.status.in_(DISPUTE_OPEN_STATUSES),
            )
        )
        if open_id is not None:
            raise _already_open(open_id)

        dispute = BookingDispute(
            booking_id=booking.id,
            raised_by_user_id=booking.consumer_id,
            created_by=actor.admin_id,
            reason_code=body.reason_code,
            description=body.description,
            status="RAISED",
        )
        try:
            async with self.db.begin_nested():
                self.db.add(dispute)
                await self.db.flush()
        except IntegrityError:
            # Another admin logged one between the check and the insert.
            open_id = await self.db.scalar(
                select(BookingDispute.id).where(
                    BookingDispute.booking_id == booking.id,
                    BookingDispute.status.in_(DISPUTE_OPEN_STATUSES),
                )
            )
            raise _already_open(open_id) from None

        await audit_admin_action(
            self.db,
            actor,
            action="dispute.create",
            target_type="dispute",
            target_id=dispute.id,
            details={"booking_id": booking.id},
            after=snapshot("dispute", dispute),
        )
        return await self._one_row(dispute.id)

    async def _locked(self, dispute_id: uuid.UUID) -> BookingDispute:
        dispute = await self.db.scalar(
            select(BookingDispute)
            .where(BookingDispute.id == dispute_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if dispute is None:
            raise DisputeNotFound()
        return dispute

    async def review(self, actor: AuditActor, dispute_id: uuid.UUID) -> AdminDisputeRow:
        dispute = await self._locked(dispute_id)
        if dispute.status != "RAISED":
            raise _invalid_state(dispute, "Only a RAISED dispute can be picked up")
        before = snapshot("dispute", dispute)
        dispute.status = "IN_REVIEW"
        await self.db.flush()
        await audit_admin_action(
            self.db,
            actor,
            action="dispute.review",
            target_type="dispute",
            target_id=dispute.id,
            details={"booking_id": dispute.booking_id},
            before=before,
            after=snapshot("dispute", dispute),
        )
        return await self._one_row(dispute.id)

    async def resolve(
        self, actor: AuditActor, admin: User, dispute_id: uuid.UUID, body: AdminDisputeResolve
    ) -> AdminDisputeRow:
        dispute = await self._locked(dispute_id)
        if dispute.status not in DISPUTE_OPEN_STATUSES:
            raise _invalid_state(dispute, "This dispute is already resolved")
        booking = await self.db.get(Booking, dispute.booking_id)
        ctx = await self._refund_context(booking, lock=True)
        refundable = ctx.refundable

        amount = self._refund_amount(body, refundable)
        if amount is not None and admin.admin_role is AdminRole.SUPPORT:
            limit = await self._support_limit()
            if amount > limit:
                raise RefundOverSupportLimit(limit, amount)

        # Razorpay first, before any field changes: a rejection or timeout
        # leaves the dispute exactly as it was.
        refund = None
        if amount is not None and ctx.channel == "RAZORPAY":
            from app.services.payment_service import PaymentService

            refund = await PaymentService(self.db).create_dispute_refund(
                payment=ctx.payment,
                dispute_id=dispute.id,
                booking_id=booking.id,
                amount_paise=to_paise(amount),
            )

        before = snapshot("dispute", dispute)
        now = datetime.now(UTC)
        dispute.resolution_type = body.resolution_type
        dispute.resolution_notes = body.notes
        dispute.resolved_by = admin.id
        dispute.resolved_at = now
        dispute.status = "RESOLVED"

        if amount is None:
            dispute.status = "CLOSED"
            dispute.closed_at = now
        elif refund is None:
            # Cash: the money moves outside the app. A record only.
            dispute.refund_channel = "MANUAL"
            dispute.refund_amount = amount
            dispute.status = "CLOSED"
            dispute.closed_at = now
        else:
            dispute.refund_channel = "RAZORPAY"
            dispute.razorpay_refund_id = refund.get("id")
            # An adopted refund (a retry after a timeout) is the money that
            # actually moved, so its amount is the one recorded.
            if isinstance(refund.get("amount"), int) and refund["amount"] > 0:
                amount = _rupees(refund["amount"])
            dispute.refund_amount = amount
            rzp_status = refund.get("status")
            if rzp_status == "processed":
                dispute.refund_status = "PROCESSED"
                dispute.status = "CLOSED"
                dispute.closed_at = now
            elif rzp_status == "failed":
                dispute.refund_status = "FAILED"
                dispute.refund_failed_reason = "Razorpay reported the refund failed"
            else:
                dispute.refund_status = "PENDING"

        await self.db.flush()
        await audit_admin_action(
            self.db,
            actor,
            action="dispute.resolve",
            target_type="dispute",
            target_id=dispute.id,
            details={
                "booking_id": booking.id,
                "refundable_before": refundable,
                "refund_amount_paise": to_paise(amount) if amount is not None else None,
            },
            reason=body.notes,
            before=before,
            after=snapshot("dispute", dispute),
        )

        booking_id = str(booking.id)
        event = PUSH_EVENT_FOR[dispute.refund_channel]
        dispute_ref = str(dispute.id)

        def _push() -> None:
            from app.tasks.notification_tasks import send_booking_notification

            send_booking_notification.delay(
                booking_id=booking_id, event=event, dispute_id=dispute_ref
            )

        call_after_commit(self.db, _push)
        return await self._one_row(dispute.id)

    @staticmethod
    def _refund_amount(body: AdminDisputeResolve, refundable: Decimal) -> Decimal | None:
        """The rupees to refund (None for NO_REFUND), or a 409 / 422."""
        sent = body.refund_amount
        if body.resolution_type == "NO_REFUND":
            if sent is not None:
                raise DisputeInvalid("refund_amount", "NO_REFUND takes no refund_amount")
            return None
        if refundable <= 0:
            raise DisputeConflict(
                "NOTHING_TO_REFUND",
                "Nothing is left to refund on this booking; resolve it as NO_REFUND",
                refundable=0.0,
            )
        if sent is not None:
            if not sent.is_finite() or sent != sent.quantize(CENT):
                raise DisputeInvalid("refund_amount", "refund_amount has at most 2 decimals")
            if sent <= 0:
                raise DisputeInvalid("refund_amount", "refund_amount must be greater than 0")
        if body.resolution_type == "FULL_REFUND":
            if sent is not None and sent != refundable:
                raise DisputeInvalid(
                    "refund_amount",
                    f"A full refund is ₹{refundable}; reload, the refundable amount changed",
                    refundable=float(refundable),
                )
            return refundable
        if sent is None:
            raise DisputeInvalid("refund_amount", "PARTIAL_REFUND needs a refund_amount")
        if sent > refundable:
            raise DisputeInvalid(
                "refund_amount",
                f"Only ₹{refundable} is left to refund",
                refundable=float(refundable),
            )
        return sent.quantize(CENT)


def _already_open(open_id) -> DisputeConflict:
    return DisputeConflict(
        "DISPUTE_ALREADY_OPEN",
        "This booking already has an open dispute",
        dispute_id=str(open_id) if open_id else None,
    )


def _invalid_state(dispute: BookingDispute, message: str) -> DisputeConflict:
    return DisputeConflict("DISPUTE_INVALID_STATE", message, status=dispute.status)


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
