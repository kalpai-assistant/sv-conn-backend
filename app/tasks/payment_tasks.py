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


@celery_app.task(name="app.tasks.payment_tasks.reconcile_payouts")
def reconcile_payouts():
    """
    Celery Beat task: reconcile our settlement state against RazorpayX's payouts.

    Repairs drift left by missed webhooks or the create-path dual-write gap. Runs
    the async reconcile in a fresh event loop + DB session; the look-back window
    overlaps previous runs so nothing slips between them.

    Provider pushes for the repairs are dispatched only after the commit succeeds: a
    run repairs many payouts in one transaction, and a push sent for a repair that
    then rolled back would tell a provider their money arrived when it hadn't.
    """
    import asyncio
    import time

    from app.config import get_settings
    from app.database import AsyncSessionLocal
    from app.services.payout_service import PayoutService

    settings = get_settings()
    now = int(time.time())
    since = now - settings.PAYOUT_RECONCILE_WINDOW_SECONDS

    async def _run():
        async with AsyncSessionLocal() as session:
            report = await PayoutService(session).reconcile_payouts(since_ts=since, until_ts=now)
            await session.commit()
            # Durable now — safe to tell the providers.
            PayoutService.dispatch_notifications(report.pop("notifications", []))
            return report

    report = asyncio.run(_run())
    return f"reconciled {report}"
