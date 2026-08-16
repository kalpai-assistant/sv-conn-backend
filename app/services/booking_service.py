"""
Booking service: state machine enforcement, commission calculation,
OTP-gated service start, and paginated listing.
"""

import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.exceptions import (
    BookingNotFound,
    BookingOfferClosed,
    InvalidOTP,
    InvalidStateTransition,
    UnauthorizedAction,
)
from app.models.address import Address
from app.models.booking import (
    VALID_TRANSITIONS,
    Booking,
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.models.booking_item import BookingItem
from app.models.booking_status_log import BookingStatusLog
from app.models.provider_profile import ProviderProfile
from app.models.service import Service
from app.models.user import UserRole
from app.schemas.booking import (
    BookingListResponse,
    BookingOfferResponse,
    BookingResponse,
    CreateBookingItemRequest,
)
from app.services.provider_matching import (
    is_eligible_provider_multi_stmt,
)
from app.utils.geo import distance_km_to_point
from app.utils.money import to_decimal

settings = get_settings()


class BookingService:
    """Core booking domain: creation, state transitions, and listing."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Address hydration
    # ------------------------------------------------------------------

    async def attach_addresses(self, bookings: Iterable[Booking]) -> None:
        """Load each booking's consumer address onto it, ready for serialisation.

        Every path that returns a ``BookingResponse`` has to call this first:
        ``BookingResponse.address`` reads ``Booking.consumer_address`` (SVC-170),
        and that relationship is declared ``lazy="raise_on_sql"`` so a path that
        forgets fails by name here instead of by ``MissingGreenlet`` inside
        pydantic.

        Fetching explicitly rather than with ``selectinload`` on the query is
        deliberate, and the two are not interchangeable at these call sites.
        ``Session.get`` answers from the identity map when it can, and a load that
        emits no SQL applies no loader options — so ``get(Booking, id,
        options=[selectinload(...)])`` silently leaves the relationship unloaded
        for any booking already in the session, which is every booking in a test
        and any booking read twice in one request. This module's existing habit of
        an explicit ``db.get(Address, ...)`` does not have that failure mode, and
        this is the batched form of it.

        One query regardless of page size, so the 50-row list route does not turn
        into 50 round-trips. ``set_committed_value`` marks the relationship
        *already loaded* rather than *changed*, which is what keeps this read-path
        hydration out of the unit of work — plain assignment would enrol every
        serialised booking in the next flush.
        """
        bookings = list(bookings)
        address_ids = {b.consumer_address_id for b in bookings if b.consumer_address_id}
        if not address_ids:
            return

        rows = await self.db.execute(select(Address).where(Address.id.in_(address_ids)))
        by_id = {address.id: address for address in rows.scalars()}
        for booking in bookings:
            set_committed_value(booking, "consumer_address", by_id.get(booking.consumer_address_id))

    # ------------------------------------------------------------------
    # Items hydration
    # ------------------------------------------------------------------

    async def attach_items(self, bookings: Iterable[Booking]) -> None:
        """Load each booking's items (with their services) ready for serialisation.

        Companion to ``attach_addresses``, and for the same reason: every path
        that returns a ``BookingResponse`` reads ``booking.items`` and each
        item reads ``item.service.name``, and both relationships are declared
        ``lazy="raise_on_sql"``. A caller who forgets fails by name here
        instead of by MissingGreenlet inside pydantic — and on the list route
        would otherwise fire one query per booking (for items) times one per
        item (for the service), a classic N+1.

        Two queries: one selectinload-style fetch for items with their
        services eager-loaded, plus one identity-map assignment per booking.
        ``set_committed_value`` marks the ``items`` relationship *already
        loaded* rather than *changed* — same trick ``attach_addresses`` uses
        to keep the read-path hydration out of the unit of work.
        """
        bookings = list(bookings)
        booking_ids = [b.id for b in bookings]
        if not booking_ids:
            return

        rows = await self.db.execute(
            select(BookingItem)
            .options(selectinload(BookingItem.service))
            .where(BookingItem.booking_id.in_(booking_ids))
        )
        by_booking: dict[uuid.UUID, list[BookingItem]] = {}
        for item in rows.scalars():
            by_booking.setdefault(item.booking_id, []).append(item)
        for booking in bookings:
            set_committed_value(booking, "items", by_booking.get(booking.id, []))

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_booking(
        self,
        consumer_id: uuid.UUID,
        items: list[CreateBookingItemRequest],
        address_id: uuid.UUID,
        scheduled_at: datetime | None = None,
        payment_method: str = "online",
        coupon_code: str | None = None,
    ) -> Booking:
        """Create a booking from a cart of one or more services.

        Matching-policy (i) — one booking, one provider, must cover *all*
        services in the cart — implies the whole cart is dispatched as one
        unit. That has two knock-on effects the caller usually doesn't think
        about but this method enforces:

        1. Every service in the cart must be active. A single inactive item
           refuses the whole cart, not just its own line — dispatching a
           booking whose second item can never be delivered would either
           strand the customer at completion time or force us to auto-cancel
           mid-lifecycle, both worse than a clean 404 upfront.
        2. The pool of providers that could ever take this booking is the
           intersection of pools that could take each service. We deliberately
           *don't* validate that intersection is non-empty here; the broadcast
           does that in ``notification_tasks``, and refusing a legally-valid
           cart at creation because no provider is currently online would
           surface "no pros nearby" as a POST failure instead of the
           tracking-screen state the frontend already handles (5.7).
        """
        if not items:
            raise ValueError("Booking must have at least one service")

        # Load every service in one query — n round-trips on ``db.get`` would
        # be silly for a two- or three-item cart, and orders of magnitude
        # worse if a client ever pushes to the ``quantity <= 10`` limit
        # across ten distinct services.
        service_ids = [i.service_id for i in items]
        rows = await self.db.execute(select(Service).where(Service.id.in_(service_ids)))
        services_by_id = {s.id: s for s in rows.scalars()}

        for item in items:
            svc = services_by_id.get(item.service_id)
            if svc is None or not svc.is_active:
                # 400 rather than 404 — the caller sent a cart the platform
                # can't fulfil, which is a request problem, not a resource one.
                raise ValueError(f"Service {item.service_id} not found or inactive")

        # Exact decimal arithmetic per line (SVC-150). Commission is the rounded
        # half, payout is the remainder — so the two always add back to exactly
        # the line total and no paisa is invented or lost in the split. Applied
        # per item so the same guarantee holds for a multi-service cart.
        commission_rate = to_decimal(settings.PLATFORM_COMMISSION_PERCENT) / 100

        # First pass: compute totals so we can seed the Booking row with the
        # aggregates. Second pass (after flush) creates the item rows keyed
        # to the booking id.
        item_lines: list[tuple[CreateBookingItemRequest, Service, Decimal, Decimal, Decimal]] = []
        for item in items:
            svc = services_by_id[item.service_id]
            unit_price = to_decimal(svc.base_price)
            total_price = to_decimal(unit_price * item.quantity)
            platform_commission = to_decimal(total_price * commission_rate)
            provider_payout = total_price - platform_commission
            item_lines.append((item, svc, total_price, platform_commission, provider_payout))

        total_service_amount = sum((line[2] for line in item_lines), start=Decimal(0))
        total_platform_commission = sum((line[3] for line in item_lines), start=Decimal(0))
        total_provider_payout = sum((line[4] for line in item_lines), start=Decimal(0))

        # Coupon application. Validated with the cart total *before* the
        # booking row is added so an invalid code refuses the request cleanly
        # (no orphan Booking rolled back). CouponInvalid propagates as 400.
        # SVC-181 moved coupon rules onto Decimal (was Float) so the discount
        # threading from validate() → apply_to_booking → booking row is one
        # exact-arithmetic type end to end — no more Decimal↔float round-trip
        # at this boundary.
        discount_amount: Decimal = Decimal("0.00")
        redeemed_coupon = None
        if coupon_code:
            from app.services.coupon_service import CouponService

            redeemed_coupon, discount_amount = await CouponService(self.db).validate(
                code=coupon_code,
                user_id=consumer_id,
                cart_amount=total_service_amount,
            )

        booking = Booking(
            consumer_id=consumer_id,
            provider_id=None,  # Unassigned until a provider accepts the broadcast
            consumer_address_id=address_id,
            status=BookingStatus.REQUESTED,
            scheduled_at=scheduled_at,
            doorstep_otp=self._generate_doorstep_otp(),
            service_amount=total_service_amount,
            platform_commission=total_platform_commission,
            provider_payout=total_provider_payout,
            # Decimal end to end since SVC-181 (was via float(); see the
            # coupon-application block above).
            discount_amount=discount_amount,
            payment_method=payment_method,
        )
        self.db.add(booking)
        await self.db.flush()

        # Redemption written inside the same transaction as the Booking, so
        # a rollback anywhere below (item insert failure, transaction abort)
        # takes the redemption with it — see the note in
        # coupon_service.apply_to_booking.
        if redeemed_coupon is not None:
            from app.services.coupon_service import CouponService

            await CouponService(self.db).apply_to_booking(
                coupon=redeemed_coupon,
                booking_id=booking.id,
                user_id=consumer_id,
                discount_amount=discount_amount,
            )
            await self.db.flush()

        # Create items in one flush after the booking id is materialised.
        # We deliberately don't rely on the ORM cascade-through-relationship
        # here — the parent doesn't have ``items`` populated yet, and setting
        # it now would count as a mutation rather than a load, tripping the
        # ``raise_on_sql`` sentinel we depend on for read-path hygiene.
        item_rows: list[BookingItem] = []
        for req, svc, total_price, platform_commission, provider_payout in item_lines:
            item_row = BookingItem(
                booking_id=booking.id,
                service_id=svc.id,
                quantity=req.quantity,
                unit_price=svc.base_price,
                total_price=total_price,
                platform_commission=platform_commission,
                provider_payout=provider_payout,
            )
            self.db.add(item_row)
            item_rows.append(item_row)
        await self.db.flush()

        # Hand the items to the relationship as *loaded* (matching how
        # attach_items would populate them), so the response builder that
        # runs immediately after doesn't fault on raise_on_sql. Bypasses the
        # unit-of-work — plain assignment would enrol them for a second
        # INSERT on the next flush.
        set_committed_value(booking, "items", item_rows)

        # The 201 carries the address like every other booking response (SVC-170).
        await self.attach_addresses([booking])

        await self._log_transition(
            booking_id=booking.id,
            from_status="none",
            to_status=BookingStatus.REQUESTED.value,
            changed_by=consumer_id,
        )

        return booking

    # ------------------------------------------------------------------
    # Fetch single booking
    # ------------------------------------------------------------------

    async def get_booking(self, booking_id: uuid.UUID, user_id: uuid.UUID) -> Booking:
        """Fetch a booking, enforcing that caller is the consumer or assigned provider."""
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)
        if user_id not in (booking.consumer_id, booking.provider_id):
            raise UnauthorizedAction("You do not have access to this booking")
        # After the access check, not before: a caller who is refused never
        # causes a lookup of the address (or items) they were refused.
        await self.attach_addresses([booking])
        await self.attach_items([booking])
        return booking

    async def get_offer(
        self, booking_id: uuid.UUID, provider_id: uuid.UUID
    ) -> BookingOfferResponse:
        """What a broadcast recipient may see about a job before accepting it.

        ``get_booking`` cannot serve this: it admits only the consumer or the
        *assigned* provider, and a broadcast recipient is neither — ``provider_id``
        stays NULL until someone accepts. Without this the provider gets a push
        carrying a booking id they are forbidden to resolve (SVC-153).

        Eligibility is checked **before** the still-open check, deliberately. The
        reverse order would tell any caller whether a given booking exists and has
        been taken; this way a provider who was never a recipient learns nothing
        beyond 403.

        With multi-service carts the eligibility check applies matching-policy
        (i): this provider must be subscribed to *every* service on the
        booking. The intersection lives in ``is_eligible_provider_multi_stmt``
        so ``notification_tasks`` (which decides who to broadcast to) and this
        endpoint (which decides who may resolve the offer) share one rule.
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)

        # Load items now — needed for the eligibility check and the summary.
        await self.attach_items([booking])

        address = await self.db.get(Address, booking.consumer_address_id)
        if address is None or address.location is None:
            # The broadcast bails out on the same condition, so nobody was ever
            # offered this booking — answer as we would to any non-recipient.
            raise UnauthorizedAction("You were not offered this booking")

        service_ids = [item.service_id for item in booking.items]
        eligible = await self.db.execute(
            is_eligible_provider_multi_stmt(service_ids, address.location, provider_id)
        )
        if eligible.scalar_one_or_none() is None:
            raise UnauthorizedAction("You were not offered this booking")

        # Open means REQUESTED — the only state a booking can be accepted from.
        # Anything else (claimed, cancelled, expired) ends the offer.
        if booking.status is not BookingStatus.REQUESTED:
            raise BookingOfferClosed()

        distance = await self.db.execute(
            select(
                distance_km_to_point(ProviderProfile.service_area_center, address.location)
            ).where(ProviderProfile.user_id == provider_id)
        )

        expires_at, seconds_remaining = self._offer_expiry(booking)

        return BookingOfferResponse(
            booking_id=booking.id,
            service_name=_summarise_service_names(booking.items),
            service_amount=booking.service_amount,
            full_address=address.full_address,
            distance_km=round(distance.scalar_one(), 2),
            expires_at=expires_at,
            seconds_remaining=seconds_remaining,
        )

    @staticmethod
    def _offer_expiry(booking: Booking) -> tuple[datetime, int]:
        """When this offer dies, and how long the card has left (SVC-166).

        Derived from ``created_at`` + the configured timeout — the same pair
        ``expire_stale_bookings`` sweeps on — so changing the timeout moves both
        the real expiry and what we advertise. Nothing is stored; there is no
        column and no migration behind this.

        Two deliberate choices:

        * The nominal deadline is reported, not the worst case. The sweep runs
          every 30s, so a booking can survive up to ``timeout + 30s`` in practice.
          Advertising that would promise time the server may not honour; the card
          giving up a little early is the safe direction.
        * ``seconds_remaining`` is floored at zero. Inside that sweep lag a
          booking is past its deadline but still REQUESTED, so this endpoint
          still serves it — a negative countdown would be nonsense, and 0 tells
          the card to dismiss.
        """
        timeout = timedelta(seconds=get_settings().BOOKING_REQUEST_TIMEOUT_SECONDS)
        expires_at = booking.created_at + timeout
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        return expires_at, max(0, int(remaining))

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def accept_booking(
        self, booking_id: uuid.UUID, provider_id: uuid.UUID
    ) -> tuple[Booking, bool]:
        """Claim a broadcast booking. Returns ``(booking, newly_accepted)``.

        Accept needs outcomes the generic state machine cannot express, so it gets
        its own entry point rather than more special cases inside
        ``transition_status`` — whose signature five other routes depend on
        (SVC-154).

        The ``newly_accepted`` flag exists for one reason: the router notifies the
        consumer after a successful accept, and an idempotent retry must not fire
        that push a second time. Returning a bare booking would make a lost
        response send "Booking Confirmed" again on every retry — worse than the
        error it replaces.

        Order matters, and each branch is a case the card has to tell apart:

        * cancelled/expired first, so a provider re-accepting a booking they had
          claimed and which was later cancelled gets 410, not a stale 200;
        * then their own accepted booking → idempotent, no state change, no push;
        * then anyone else's → 403, the lost race. Checked before the transition
          guard, which would otherwise always fire first and report the useless
          "cannot move from accepted to accepted".

        A provider whose booking has moved past ACCEPTED (en route, in progress)
        falls through to the transition guard and still gets 409 — re-accepting
        there is a genuinely illegal move, not a retry.
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)

        # SVC-168: eligibility check before we reveal anything about the booking.
        # `get_offer` uses the same shared predicate
        # (``is_eligible_provider_multi_stmt``), so the set of providers who
        # may accept can never drift from the set who were offered — the
        # invariant SVC-153 established. For a multi-service cart the
        # provider must cover *every* item (matching-policy (i)).
        #
        # Skip the check when the caller already owns the booking: they were
        # eligible when they accepted, and re-checking would refuse a legitimate
        # idempotent retry from a provider who has since gone offline / had KYC
        # revoked / unsubscribed from the service.
        if booking.provider_id != provider_id:
            address = await self.db.get(Address, booking.consumer_address_id)
            if address is None or address.location is None:
                # Address gone / never geocoded → the broadcast bailed too, so
                # nobody was ever offered this. Refuse like any non-recipient.
                raise UnauthorizedAction("You were not offered this booking")
            # Load items — the eligibility check needs the full set.
            await self.attach_items([booking])
            service_ids = [item.service_id for item in booking.items]
            eligible = await self.db.execute(
                is_eligible_provider_multi_stmt(service_ids, address.location, provider_id)
            )
            if eligible.scalar_one_or_none() is None:
                # 403 here is correct — this is "you should never have been able
                # to do this" (SVC-168), which is different from "you lost the
                # race" (410, below) or "the booking's over" (also 410). All
                # three are distinguishable by status code alone.
                raise UnauthorizedAction("You were not offered this booking")

        if booking.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED):
            raise BookingOfferClosed()

        if booking.provider_id == provider_id:
            if booking.status is BookingStatus.ACCEPTED:
                # Idempotent retry: returns without reaching `transition_status`,
                # so this is the one accept path that has to hydrate for itself.
                await self.attach_addresses([booking])
                await self.attach_items([booking])
                return booking, False
        elif booking.provider_id is not None:
            # SVC-167: another provider won the race. From the card's point of
            # view this is the same outcome as cancelled/expired — the offer is
            # gone, dismiss it quietly. Was 403; the code drift with the
            # cancelled/expired path forced the frontend into
            # `if (403 || 410) dismiss()`. 410 unifies the vocabulary.
            raise BookingOfferClosed()

        booking = await self.transition_status(
            booking_id=booking_id,
            new_status=BookingStatus.ACCEPTED,
            changed_by=provider_id,
        )
        return booking, True

    async def transition_status(
        self,
        booking_id: uuid.UUID,
        new_status: BookingStatus,
        changed_by: uuid.UUID,
        otp: str | None = None,
        cancellation_reason: str | None = None,
        metadata: dict | None = None,
    ) -> Booking:
        """
        Enforce state machine transitions.
        Raises InvalidStateTransition if the move is illegal.
        """
        # Acquire a row-level DB lock to safely handle concurrent acceptance attempts
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)

        allowed = VALID_TRANSITIONS.get(booking.status, [])
        if new_status not in allowed:
            raise InvalidStateTransition(
                f"Cannot move from {booking.status.value} to {new_status.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )

        # Broadcasting logic: The first provider to accept claims the booking globally
        if new_status == BookingStatus.ACCEPTED:
            if booking.provider_id is None:
                booking.provider_id = changed_by
            elif booking.provider_id != changed_by:
                raise UnauthorizedAction(
                    "This booking has already been claimed by another provider"
                )
        elif changed_by not in (booking.provider_id, booking.consumer_id):
            # For other transitions (start, complete), only the assigned parts can mutate
            raise UnauthorizedAction(
                "Only the assigned provider or consumer can perform this action"
            )

        # OTP gate for doorstep service start
        if new_status == BookingStatus.IN_PROGRESS:
            if not otp or otp != booking.doorstep_otp:
                raise InvalidOTP("Doorstep OTP does not match")

        if new_status == BookingStatus.CANCELLED:
            booking.cancellation_reason = cancellation_reason

        old_status = booking.status
        booking.status = new_status

        # SVC-178: cancel → refund + provider-comp policy.
        # Ordering matters — the refund initiation reads `old_status` to
        # decide whether to charge the cancellation fee. Runs BEFORE the OTP
        # wipe and the completed-side hooks below so a failure here rolls
        # back the whole transition (kept atomic with the state change).
        if new_status == BookingStatus.CANCELLED:
            await self._apply_cancellation_policy(booking, old_status)

        # Zero the doorstep OTP as soon as the transition it protects has
        # happened. Consumer's view of the OTP is stale from this point
        # anyway, and there's no legitimate reason to keep a plaintext
        # 4-digit secret live in the DB row for the life of the booking —
        # a DB dump would expose every used OTP alongside consumer id.
        # Same treatment on CANCELLED (the OTP is now meaningless).
        if new_status in (
            BookingStatus.IN_PROGRESS,
            BookingStatus.COMPLETED,
            BookingStatus.CANCELLED,
        ):
            booking.doorstep_otp = None

        if new_status == BookingStatus.COMPLETED:
            # Only reset for cash — `process_cash_payment` is enqueued after
            # completion and needs PENDING to flip → PAID once the provider
            # confirms receipt; its own early-return-on-PAID depends on this
            # reset happening for cash. For online, PAID is set by
            # `verify_payment` (or the payments webhook) before COMPLETED, and
            # wiping it here on every completion erased the record at exactly
            # the moment the booking became payout-eligible — this is SVC-39
            # G9. Gate on CASH so online payments survive completion.
            if booking.payment_method is PaymentMethod.CASH:
                booking.payment_status = PaymentStatus.PENDING
            else:
                # SVC-193: online booking completing → provider is owed their
                # payout share (plus any tip already on the row; a later tip
                # is an update on booking.tip_amount, not a separate ledger
                # entry — Q4 "defer to payout" means the payout math sums
                # provider_payout + tip_amount from the booking directly).
                # Only fired for online because a cash provider was paid at
                # the door — no platform debt was ever incurred. Rides the
                # request transaction with the transition write above.
                if booking.provider_id is not None:
                    from app.services.ledger_service import credit_booking_completed
                    from app.utils.money import to_paise

                    amount_paise = to_paise(booking.provider_payout + booking.tip_amount)
                    if amount_paise > 0:
                        await credit_booking_completed(
                            self.db,
                            provider_id=booking.provider_id,
                            booking_id=booking.id,
                            amount_paise=amount_paise,
                            note=f"booking {booking.id} completed",
                        )

                # SVC-177 (§4 T+1 gate) — dev-mode escape hatch. When the
                # setting is on (beta default), stamp settled_at here so the
                # credit above starts counting toward the wallet immediately.
                # Prod flips this off once Razorpay's `settlement.processed`
                # webhook is subscribed; that handler stamps settled_at when
                # the money actually reaches our bank account.
                if settings.AUTO_SETTLE_ON_COMPLETION and booking.settled_at is None:
                    booking.settled_at = datetime.now(UTC)
            # Fix #6: increment provider completed jobs counter
            await self._increment_completed_jobs(booking.provider_id)

        await self._log_transition(
            booking_id=booking.id,
            from_status=old_status.value,
            to_status=new_status.value,
            changed_by=changed_by,
            metadata=metadata,
        )

        # Every transition route answers with a BookingResponse, which now
        # renders both `address` (SVC-170) and `items`.
        await self.attach_addresses([booking])
        await self.attach_items([booking])

        return booking

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def list_bookings(
        self,
        user_id: uuid.UUID,
        role: UserRole,
        status_filter: str | None,
        page: int,
        per_page: int,
    ) -> BookingListResponse:
        if role == UserRole.CONSUMER:
            where = Booking.consumer_id == user_id
        else:
            where = Booking.provider_id == user_id

        stmt = select(Booking).where(where)
        count_stmt = select(func.count()).select_from(Booking).where(where)

        if status_filter:
            try:
                status_enum = BookingStatus(status_filter)
                stmt = stmt.where(Booking.status == status_enum)
                count_stmt = count_stmt.where(Booking.status == status_enum)
            except ValueError:
                pass  # Ignore unknown status filters rather than crashing

        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = stmt.offset((page - 1) * per_page).limit(per_page)
        result = await self.db.execute(stmt)
        bookings = list(result.scalars().all())

        # One query for the whole page for each relationship, not one per row —
        # the address AND the items list both render on every row of the
        # bookings tab, and this route serves up to 50. Each of these was N+1
        # waiting to happen; SVC-170 introduced the address helper for the
        # same reason and the item helper follows the pattern.
        await self.attach_addresses(bookings)
        await self.attach_items(bookings)

        return BookingListResponse(
            # Per-row redaction, not a role check: `role` says what kind of user
            # is asking, `for_viewer` asks whether *this* caller owns the booking
            # they're being shown. A provider's page is filtered to their own
            # jobs, so every row here is one where they are not the consumer and
            # the PIN must be blanked (SVC-169).
            bookings=[BookingResponse.for_viewer(b, user_id) for b in bookings],
            total=total,
            page=page,
            per_page=per_page,
        )

    # ------------------------------------------------------------------
    # Tip
    # ------------------------------------------------------------------

    async def set_tip(
        self, booking_id: uuid.UUID, consumer_id: uuid.UUID, amount: float
    ) -> Booking:
        """Set the tip on a completed booking.

        Only the booking's own consumer may set a tip, and only after
        completion — before that the provider is still on the way (or
        working) and the tip amount would be a moving guarantee we can't
        enforce. A tip is settable more than once as long as the payout
        hasn't fired yet: consumers change their minds, and re-submitting
        replaces rather than accumulates.

        Bounds and non-negativity live in the request schema; here we only
        enforce access + state.
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)
        if booking.consumer_id != consumer_id:
            raise UnauthorizedAction("Only the booking's consumer can add a tip")
        if booking.status is not BookingStatus.COMPLETED:
            raise InvalidStateTransition("Tips are accepted only after the booking is completed")
        # Once payout has fired the tip is gone from us — the money has
        # left the platform's control. Refuse further edits.
        if booking.payment_status is PaymentStatus.REFUNDED:
            raise InvalidStateTransition("Cannot tip a refunded booking")

        # SVC-39 G31: tip_amount is Numeric; `to_decimal` rounds half-up to 2dp.
        booking.tip_amount = to_decimal(amount)
        await self.db.flush()

        await self.attach_addresses([booking])
        await self.attach_items([booking])
        return booking

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _log_transition(
        self,
        booking_id: uuid.UUID,
        from_status: str,
        to_status: str,
        changed_by: uuid.UUID,
        metadata: dict | None = None,
    ) -> None:
        log = BookingStatusLog(
            booking_id=booking_id,
            from_status=from_status,
            to_status=to_status,
            changed_by=changed_by,
            metadata_=metadata or {},
        )
        self.db.add(log)

    async def _increment_completed_jobs(self, provider_user_id: uuid.UUID) -> None:
        """Atomically increment total_completed_jobs on the provider's profile."""
        from sqlalchemy import update

        await self.db.execute(
            update(ProviderProfile)
            .where(ProviderProfile.user_id == provider_user_id)
            .values(total_completed_jobs=ProviderProfile.total_completed_jobs + 1)
        )

    @staticmethod
    def _generate_doorstep_otp() -> str:
        return "".join(str(secrets.randbelow(10)) for _ in range(4))

    async def _apply_cancellation_policy(
        self,
        booking: Booking,
        old_status: BookingStatus,
    ) -> None:
        """Refund + provider-comp on cancel (SVC-178 / PAYMENT_FLOW §7).

        Policy (Q3 resolved: flat ₹49, 100% to provider):

        - Pre-acceptance (`REQUESTED` → CANCELLED): full refund, no fee, no
          provider comp — provider never blocked out their time.
        - Post-acceptance (`ACCEPTED` / `EN_ROUTE` → CANCELLED): partial
          refund (`captured − CANCELLATION_FEE_PAISE`), fee credits the
          provider's wallet in full.
        - `IN_PROGRESS+` → CANCELLED: unreachable via VALID_TRANSITIONS (only
          from REQUESTED/ACCEPTED/EN_ROUTE), but defensive: no auto-refund,
          admin handles.

        Cash bookings: no online payment to refund; no ledger writes here
        (SVC-180 handles cash-commission netting for the platform side).
        Refund path is skipped entirely.
        """
        # Cash: no money to refund customer-side. Nothing to do here.
        if booking.payment_method is not PaymentMethod.ONLINE:
            return
        # Only PAID bookings have money captured to refund. If the consumer
        # never paid (PENDING) the cancel is a pure state change.
        if booking.payment_status is not PaymentStatus.PAID:
            return

        # Captured amount = what create_order actually charged (SVC-39 G22:
        # service_amount − discount_amount, not the full service_amount).
        from app.utils.money import to_paise

        captured_paise = to_paise(booking.service_amount - booking.discount_amount)

        # Fee policy by from-status.
        if old_status is BookingStatus.REQUESTED:
            fee_paise = 0
        elif old_status in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE):
            fee_paise = settings.CANCELLATION_FEE_PAISE
            # Cap the fee at the captured amount — a coupon-heavy booking
            # can have captured < fee; charging more than we hold would
            # produce a negative refund we can't send.
            fee_paise = min(fee_paise, captured_paise)
        else:
            # Defensive — VALID_TRANSITIONS blocks this today, but if it ever
            # opens up (admin cancel, say), fail closed rather than refund
            # the full amount for a job that had already started.
            return

        refund_paise = captured_paise - fee_paise

        # Fee credit: 100% of the fee goes to the provider (Q3 resolved).
        # Runs before the refund call so the ledger stays consistent even if
        # the Razorpay call raises — the caller's transaction rolls both
        # back atomically. Only fires when there's an assigned provider,
        # which post-acceptance always is.
        if fee_paise > 0 and booking.provider_id is not None:
            from app.services.ledger_service import credit_booking_completed

            await credit_booking_completed(
                self.db,
                provider_id=booking.provider_id,
                booking_id=booking.id,
                amount_paise=fee_paise,
                note=(
                    f"cancellation fee: booking {booking.id} cancelled from " f"{old_status.value}"
                ),
            )

        if refund_paise > 0:
            from app.services.payment_service import PaymentService

            await PaymentService(self.db).initiate_refund(
                booking=booking,
                amount_paise=refund_paise,
                reason=f"cancellation from {old_status.value}",
            )


def _summarise_service_names(items: Iterable) -> str:
    """One-line summary of a cart for a push body or offer card.

    "Deep Clean" for a single-item booking; "Deep Clean + 2 more" once the
    cart grows. Kept as a module-level helper (not a BookingItem method) so
    both the offer response and the notification body can call it without
    the notification worker needing to touch the ORM instance methods —
    the worker runs sync against ``SyncSessionLocal`` and imports as little
    ORM surface as possible.
    """
    items = list(items)
    if not items:
        return "Service"
    first_name = items[0].service.name
    if len(items) == 1:
        return first_name
    return f"{first_name} + {len(items) - 1} more"
