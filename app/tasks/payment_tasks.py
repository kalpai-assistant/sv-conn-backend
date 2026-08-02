"""
Payment Celery tasks: handles cash payment recording after booking completion.
"""

from app.models.booking import Booking, PaymentStatus
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.tasks.celery_app import celery_app


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    name="app.tasks.payment_tasks.process_cash_payment",
)
def process_cash_payment(self, booking_id: str):
    """
    Record a cash payment after provider confirms collection.
    Called when booking is completed with payment_method=cash.
    Platform commission is tracked as a running balance deduction.
    """
    from app.database import SyncSessionLocal

    try:
        with SyncSessionLocal() as db:
            booking = db.get(Booking, booking_id)
            if not booking or booking.payment_status == PaymentStatus.PAID:
                return  # Idempotent

            payment = Payment(
                booking_id=booking.id,
                amount=booking.service_amount,
                platform_fee=booking.platform_commission,
                provider_settlement=booking.provider_payout,
                status=PaymentRecordStatus.CAPTURED,
                method=PaymentRecordMethod.CASH,
            )
            db.add(payment)
            booking.payment_status = PaymentStatus.PAID
            db.commit()

    except Exception as exc:
        self.retry(exc=exc)
