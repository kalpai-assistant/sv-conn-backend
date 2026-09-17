"""
Notification Celery tasks — handles async push + SMS dispatch for booking events.
Uses a synchronous DB session (SyncSessionLocal) because Celery workers are not async.
"""

import logging

from firebase_admin import messaging as _fcm_messaging

from app.models.booking import BookingStatus
from app.tasks.celery_app import celery_app
from app.utils.fcm_client import send_multicast, send_push_notification

logger = logging.getLogger(__name__)

# FCM error classes that mean this token will never succeed again — so clearing
# `users.fcm_token` is safe and the next broadcast's SVC-276 eligibility filter
# will exclude the provider by construction (SVC-301). Anything not in this
# tuple stays raised and either is caught by the caller or retries the task.
#
# * ``UnregisteredError`` — token was deleted server-side (uninstall, expiry,
#   OS revocation) or never registered.
# * ``SenderIdMismatchError`` — token belongs to a different Firebase project;
#   whoever registered it against ours cannot receive from us. Rare but
#   definitively dead: no recovery without a re-register from the app.
#
# Deliberately NOT included: ``InvalidArgumentError``. FCM v1 raises it for a
# malformed *request* too (bad payload, unknown field), not only bad tokens —
# treating it as dead would nuke good tokens for a bug in our own send code.
_DEAD_TOKEN_ERRORS: tuple[type[Exception], ...] = (
    _fcm_messaging.UnregisteredError,
    _fcm_messaging.SenderIdMismatchError,
)


def _clear_dead_fcm_token(db, user, error: Exception) -> None:
    """Null a User's ``fcm_token`` after FCM confirmed the token is dead.

    Commits inside the caller's session so the row is durable before we move
    on to the next recipient — otherwise a later exception in the same task
    would roll the clear back and we would re-hit the same dead token on the
    task's retry. The commit is scoped to this small update; the surrounding
    broadcast writes (booking-offer rows, etc.) are already committed by the
    time the per-recipient send loop runs, so nothing else is in flight to
    accidentally land.
    """
    logger.info(
        "fcm_token_cleared_after_dead_response user_id=%s error_class=%s",
        user.id,
        type(error).__name__,
    )
    user.fcm_token = None
    db.commit()


# Android notification channel for new job requests (SVC-278 issue 6).
#
# Must match `JOB_REQUEST_CHANNEL_ID` in the app's `notification-channels.ts`.
# The app creates the channel (with its own sound and MAX importance); this
# names it so the push is filed against it. A mismatch fails silently — Android
# falls back to the default channel and the job request sounds like everything
# else, which is the bug this exists to fix.
#
# The `_vN` suffix is load-bearing: Android channels are immutable after
# creation, so changing the sound means shipping a new id on both sides.
JOB_REQUEST_CHANNEL_ID = "job_requests_v1"

NOTIFICATION_TEMPLATES: dict[str, dict[str, str]] = {
    "new_request": {
        "title": "New Booking Request!",
        # {service} is filled per booking — see build_new_request_body.
        "body": "{service} — accept within 2 minutes.",
    },
    "accepted": {
        "title": "Booking Confirmed",
        "body": "Your provider has accepted the booking and will arrive soon.",
    },
    "in_progress": {
        "title": "Service Started",
        "body": "Your service is now in progress.",
    },
    "completed": {
        "title": "Service Completed",
        "body": "Your service has been completed. Please proceed to payment.",
    },
    # Goes to whichever party did not cancel — see send_booking_notification.
    # Worded to read correctly either way: "the other side pulled out" is true
    # for a provider whose job vanished and for a consumer whose provider
    # dropped it, without naming a role the recipient might not be.
    "cancelled": {
        "title": "Booking Cancelled",
        "body": "This booking has been cancelled. No further action is needed.",
    },
}


# Provider-facing payout templates, keyed by SettlementStatus value.
#
# Copy is deliberately neutral for the unhappy states: a failed or reversed payout
# is almost always an internal/bank-side issue we resolve ourselves (re-queued by an
# admin), so telling the provider "payout failed" invites support calls about
# something they cannot act on. They are told we're on it instead.
PAYOUT_NOTIFICATION_TEMPLATES: dict[str, dict[str, str]] = {
    "processing": {
        "title": "Payout On Its Way",
        "body": "Your payout of {amount} has been initiated and should reach your account shortly.",
    },
    "completed": {
        "title": "Payout Received",
        "body": "{amount} has been credited to your account.",
    },
    "failed": {
        "title": "Payout Update",
        "body": "We're still processing your payout of {amount}. We'll update you shortly.",
    },
    "reversed": {
        "title": "Payout Update",
        "body": "Your payout of {amount} was returned to us. We're looking into it "
        "and will update you shortly.",
    },
}


def _format_rupees(amount_paise: int) -> str:
    """Format a paise amount as rupees for notification copy, Indian-style.

    Indian digit grouping, not Western: the last three digits group together and
    everything above them groups in pairs — ₹10,00,000, not ₹1,000,000. Providers
    read these as lakhs; Western grouping misreads at a glance in exactly the range
    where a weekly batch payout lands.

    Integer arithmetic throughout, so the paise never round on the way to the string.
    """
    rupees, paise = divmod(int(amount_paise), 100)
    digits = str(rupees)

    if len(digits) > 3:
        head, last3 = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            head, pair = head[:-2], head[-2:]
            pairs.insert(0, pair)
        grouped = ",".join(([head] if head else []) + pairs + [last3])
    else:
        grouped = digits

    return f"₹{grouped}" if paise == 0 else f"₹{grouped}.{paise:02d}"


def build_new_request_body(service_name: str | None) -> str:
    """Body for the broadcast push: what the job is, and nothing more (SVC-153).

    Naming the service lets a provider decide whether the notification is worth
    opening. The **amount and address deliberately stay out**:

    * the address would be delivered to every nearby provider, including all the
      ones who lose the race, and would persist in OS notification logs — the
      offer endpoint hands it only to eligible providers and stops once the job
      is taken;
    * the amount would have to be rendered as a ₹ string here, and this branch has
      no money formatter. Indian digit grouping (₹10,00,000, not ₹1,000,000) is
      only visibly different above ₹1,00,000, so a naive f-string would look
      correct in review and in every realistic test, then misread for a provider
      one day. ``GET /bookings/{id}/offer`` returns the number and the app formats
      it. See the money note on SVC-153.

    Falls back to generic copy when the service is missing — an FK makes that
    unreachable today, but a push that renders "{service}" literally would be
    worse than a vague one.
    """
    return NOTIFICATION_TEMPLATES["new_request"]["body"].format(
        service=service_name or "A new service request",
    )


def _expire_unbroadcastable(db, booking, reason: str) -> None:
    """Terminate a booking the broadcast can never reach (SVC-278 issue 5).

    Three conditions make a REQUESTED booking undeliverable the moment it is
    created: no address location, an empty cart, or no eligible provider. Each
    used to `return` here, leaving the row at REQUESTED until
    `expire_stale_bookings` swept it two minutes later — so the consumer
    watched "Finding nearby partner…" for the full window to learn something
    that was already decided.

    EXPIRED is the state the tracking screen already renders as "No partners
    found nearby", so expiring now moves that message to the consumer's next
    4s poll and needs no new UI.

    `reason` is logged rather than shown: the three causes need different
    engineering responses (a broken address, a bad cart, genuinely no supply)
    but read identically to a consumer, who can only try again.
    """
    logger.info(
        "booking.unbroadcastable booking_id=%s reason=%s",
        booking.id,
        reason,
    )
    booking.status = BookingStatus.EXPIRED
    db.commit()


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    name="app.tasks.notification_tasks.send_booking_notification",
)
def send_booking_notification(self, booking_id: str, event: str, actor_id: str | None = None):
    """Send push notification for booking lifecycle events.

    ``actor_id`` is who caused the event, and only ``cancelled`` needs it:
    either party can cancel, so the recipient is "the other one". Optional so
    the events with a fixed direction (a provider accepts, a consumer is told)
    keep calling this unchanged.
    """
    from app.database import SyncSessionLocal
    from app.models.booking import Booking

    try:
        with SyncSessionLocal() as db:
            booking = db.get(Booking, booking_id)
            if not booking:
                return

            template = NOTIFICATION_TEMPLATES.get(event)
            if not template:
                return

            body = template["body"]

            if event == "new_request":
                from sqlalchemy.orm import selectinload

                from app.models.address import Address
                from app.models.booking_item import BookingItem
                from app.services.booking_service import _summarise_service_names
                from app.services.provider_matching import eligible_providers_multi_stmt

                # Get the booking address location directly
                address = db.get(Address, booking.consumer_address_id)
                if not address or not address.location:
                    _expire_unbroadcastable(db, booking, "no-address-location")
                    return

                # Load the cart items (with each item's service) so the summary
                # can name the first service. Explicit fetch — booking.items on
                # the ORM object is ``raise_on_sql``, and even if it weren't,
                # the sync worker doesn't get the async ``attach_items`` helper.
                from sqlalchemy import select as sync_select

                items = (
                    db.execute(
                        sync_select(BookingItem)
                        .options(selectinload(BookingItem.service))
                        .where(BookingItem.booking_id == booking.id)
                    )
                    .scalars()
                    .all()
                )
                body = build_new_request_body(_summarise_service_names(items))

                # Matching-policy (i): a provider must cover every service in
                # the cart to receive the push. Same predicate the async
                # ``get_offer`` uses to decide who may resolve it — SVC-153
                # keeps the broadcast set and accept set aligned.
                service_ids = [item.service_id for item in items]
                if not service_ids:
                    _expire_unbroadcastable(db, booking, "empty-cart")
                    return
                stmt = eligible_providers_multi_stmt(service_ids, address.location)
                recipients = db.execute(stmt).scalars().all()

                # SVC-254: persist the offer before pushing.
                #
                # The push is best-effort — a stale token, a denied permission
                # or an iOS Safari tab drops it with no error anywhere — so the
                # row, not the push, is what actually makes this job reachable.
                # It backs ``GET /providers/me/offers``, which a provider polls
                # when no notification arrives.
                #
                # Written *before* the pushes on purpose: a push that landed
                # ahead of its row would point the card at an offer the
                # provider cannot see yet.
                #
                # ``on_conflict_do_nothing`` keeps this idempotent against
                # ``uix_booking_offer``. This task retries up to three times, so
                # without it a retry after a partial failure would offer the
                # same provider the same booking twice.
                if not recipients:
                    # Nobody is eligible, and we know it now.
                    #
                    # What this gives up: a provider who becomes available
                    # mid-window could previously still accept, because
                    # `get_offer` re-checks eligibility live rather than
                    # reading these offer rows. Of the conditions
                    # `provider_matching` applies, availability is the only one
                    # that can change inside two minutes — so that is the whole
                    # of the loss, and it is worth less than making every
                    # consumer sit out a dead window.
                    _expire_unbroadcastable(db, booking, "no-eligible-provider")
                    return

                import uuid as _uuid

                from sqlalchemy.dialects.postgresql import insert as pg_insert

                from app.models.booking_offer import BookingOffer

                db.execute(
                    pg_insert(BookingOffer)
                    .values(
                        [
                            {
                                "id": _uuid.uuid4(),
                                "booking_id": booking.id,
                                "provider_id": recipient.id,
                            }
                            for recipient in recipients
                        ]
                    )
                    .on_conflict_do_nothing(constraint="uix_booking_offer")
                )
                db.commit()
            elif event == "cancelled":
                # Tell whoever did NOT do the cancelling. Both parties can
                # cancel, so this is the one event with no fixed direction —
                # and getting it wrong means telling someone their own action
                # happened while the person waiting on them hears nothing.
                #
                # `provider` is None until someone accepts (VALID_TRANSITIONS
                # allows REQUESTED → CANCELLED), so a consumer cancelling
                # before acceptance has nobody to notify. That is correct, not
                # a gap: no provider has committed to the job yet. The
                # `if recipient` guard below already handles it.
                recipients = (
                    [booking.consumer]
                    if actor_id and str(booking.provider_id) == actor_id
                    else [booking.provider]
                )
            else:
                recipients = [booking.provider if event == "accepted" else booking.consumer]

            for recipient in recipients:
                if not (recipient and getattr(recipient, "fcm_token", None)):
                    continue
                try:
                    send_push_notification(
                        token=recipient.fcm_token,
                        title=template["title"],
                        body=body,
                        # Only the job request gets its own channel. Giving
                        # every event one would leave a provider with a wall of
                        # equally urgent sounds and nothing distinguishing the
                        # one that pays them.
                        android_channel_id=(
                            JOB_REQUEST_CHANNEL_ID if event == "new_request" else None
                        ),
                        # Payload stays a bare pointer — the card fetches its data
                        # from the offer endpoint, which can revoke access once the
                        # booking is claimed. A push cannot be taken back.
                        data={"booking_id": booking_id, "event": event},
                    )
                except _DEAD_TOKEN_ERRORS as dead:
                    # SVC-301: FCM has confirmed this token can never succeed.
                    # Null it so the next broadcast's SVC-276 filter excludes
                    # this provider — the fix compounds — and carry on to the
                    # remaining recipients rather than letting one dead token
                    # abort the whole broadcast (which the pre-SVC-301
                    # outer-only try/except used to do).
                    _clear_dead_fcm_token(db, recipient, dead)

    except Exception as exc:
        self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    name="app.tasks.notification_tasks.send_payout_notification",
)
def send_payout_notification(
    self,
    provider_id: str,
    event: str,
    amount_paise: int,
    payout_id: str | None = None,
):
    """Send a provider push notification for a payout lifecycle event.

    One push per *payout*, not per booking: a payout settles a whole batch of the
    provider's completed bookings, so the recipient is the provider and the copy
    carries the batch total. ``event`` is a ``SettlementStatus`` value passed in by
    the caller rather than re-read from the DB, so the message always describes the
    transition that triggered it.

    No-ops when the provider has no FCM token (single-device: the model has one
    ``user.fcm_token``).
    """
    from app.database import SyncSessionLocal
    from app.models.user import User

    try:
        template = PAYOUT_NOTIFICATION_TEMPLATES.get(event)
        if not template:
            return

        with SyncSessionLocal() as db:
            provider = db.get(User, provider_id)
            if not provider or not provider.fcm_token:
                return

            data = {"type": "payout", "event": event}
            if payout_id:
                data["payout_id"] = payout_id

            try:
                send_push_notification(
                    token=provider.fcm_token,
                    title=template["title"],
                    body=template["body"].format(amount=_format_rupees(amount_paise)),
                    data=data,
                )
            except _DEAD_TOKEN_ERRORS as dead:
                # SVC-301. Same reasoning as the booking-broadcast send loop —
                # clear once, let the missed push be caught by whatever the
                # provider looks at next (offers list, payout history screen).
                _clear_dead_fcm_token(db, provider, dead)

    except Exception as exc:
        self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=1,
    default_retry_delay=30,
    name="app.tasks.notification_tasks.broadcast_update_available",
)
def broadcast_update_available(self, version: str, platform: str = "android"):
    """Push a "new version available" nudge to every user's device (SVC-227).

    Complements SVC-162's poll-based version check: the poll only fires
    when the user opens the app, so a tester who hasn't opened it in a
    week doesn't learn about a patch release. This push lands in the
    notification shade regardless of whether the app is open, and tapping
    it brings the app to foreground where ``useAppVersionCheck`` re-runs
    and renders the nudge/block modal SVC-162 already implements.

    No per-user targeting today — every consumer with an FCM token on the
    given platform gets it. A user already on the latest version will get
    the push and their in-app check will silently show nothing (state:
    ok), which is a tiny UX blemish that saves us tracking installed
    version per token. Worth revisiting when the user base grows past
    "we can afford to notify everyone."

    Failed sends are logged but not retried at the per-token level —
    FCM already retries transient failures internally and a stale token
    is a soft failure the next FCM registration will fix.
    """
    from sqlalchemy import and_, select

    from app.database import SyncSessionLocal
    from app.models.user import User, UserRole

    try:
        with SyncSessionLocal() as db:
            # Consumers only for now — providers get their own comms via
            # booking/payout pushes. Widening to providers is a one-line
            # change if we want it.
            rows = db.execute(
                select(User.fcm_token).where(
                    and_(
                        User.fcm_token.is_not(None),
                        User.role == UserRole.CONSUMER,
                    )
                )
            ).all()
            tokens = [t[0] for t in rows if t[0]]
            if not tokens:
                return {"success_count": 0, "failure_count": 0, "recipient_count": 0}

            summary = send_multicast(
                tokens=tokens,
                title="Update available",
                body=f"EazyServ v{version} is ready. Tap to update.",
                # The app doesn't need the version in the payload — tapping
                # opens the app and the existing version-check hook fetches
                # /downloads/latest.json for itself. `type` lets a future
                # in-app handler branch (e.g. deep-link to a release-notes
                # screen) without a schema change.
                data={"type": "update_available", "version": version, "platform": platform},
            )
            summary["recipient_count"] = len(tokens)
            return summary
    except Exception as exc:
        # One retry (default_retry_delay=30s) — the DB was probably
        # blipping. Multicast internally handles per-token transient
        # failures.
        self.retry(exc=exc)
