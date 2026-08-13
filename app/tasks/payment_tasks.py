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


@celery_app.task(
    bind=True,
    max_retries=0,
    name="app.tasks.payment_tasks.execute_provider_payout",
)
def execute_provider_payout(self, provider_id: str, admin_user_id: str):
    """
    SVC-188 (G30): actually run the RazorpayX payout, off the request path.

    The admin endpoint enqueues this task and returns 202 immediately, so the
    web worker no longer holds a DB connection through the 30-90s RazorpayX
    HTTP call and the SELECT-FOR-UPDATE lock on the provider's pending bookings.
    Everything else about the flow is unchanged: this task calls the same
    ``PayoutService.create_provider_payout`` the endpoint used to call
    synchronously, so the audit-then-Razorpay-then-DB write ordering (and
    every guarantee that hangs off it — SVC-172's open-attempt gate, the
    idempotency key derived from the batch, the ``unknown`` audit outcome
    on exception) is identical.

    ``max_retries=0``: Celery-level retry would fire a fresh attempt against
    a possibly-completed payout. The reconciliation loop
    (``reconcile_payouts``, nightly) is the safe recovery path — it queries
    RazorpayX by ``reference_id`` (= our idempotency key) and reflects the
    real outcome into the audit and booking rows.
    """
    import asyncio
    import uuid

    from app.database import AsyncSessionLocal
    from app.services.payout_service import PayoutService

    async def _run():
        async with AsyncSessionLocal() as session:
            service = PayoutService(session)
            try:
                result = await service.create_provider_payout(
                    uuid.UUID(provider_id), uuid.UUID(admin_user_id)
                )
                await session.commit()
                # Only drain pushes after the transaction is durable — matches the
                # comment in create_provider_payout: money already moved at RazorpayX,
                # so a push before commit could tell the provider about a payout the
                # DB failed to record.
                await service.dispatch_pending_notifications()
                return result
            except Exception:
                await session.rollback()
                raise

    return asyncio.run(_run())


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
