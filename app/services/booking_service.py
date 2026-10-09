"""
Booking service: state machine enforcement, commission calculation,
OTP-gated service start, and paginated listing.
"""

import logging
import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import status
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.exceptions import (
    AppException,
    BookingNotFound,
    BookingOfferClosed,
    InvalidOTP,
    InvalidStateTransition,
    UnauthorizedAction,
)
from app.models.address import Address
from app.models.booking import (
    ACTIVE_JOB_STATUSES,
    VALID_TRANSITIONS,
    Booking,
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.models.booking_assignment_event import BookingAssignmentEvent
from app.models.booking_item import BookingItem
from app.models.booking_offer import BookingOffer, OfferResponse
from app.models.booking_status_log import BookingStatusLog
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.user import User, UserRole
from app.schemas.booking import (
    AssignmentTimelineItem,
    BookingListResponse,
    BookingOfferResponse,
    BookingResponse,
    BookingTimelineItem,
    CreateBookingItemRequest,
    StatusTimelineItem,
    TimelinePro,
)
from app.services.provider_matching import (
    is_eligible_provider_multi_stmt,
)
from app.services.provider_service import ProviderService
from app.utils.geo import distance_km_to_point
from app.utils.money import to_decimal

settings = get_settings()
logger = logging.getLogger(__name__)

# Stable codes for two-level dispatch (SVC-320 PR 2).
PRO_NOT_FOUND = "PRO_NOT_FOUND"
PRO_BUSY = "PRO_BUSY"
PRO_OFFLINE = "PRO_OFFLINE"
PRO_KYC_INCOMPLETE = "PRO_KYC_INCOMPLETE"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
PROVIDER_EN_ROUTE = "PROVIDER_EN_ROUTE"
# SVC-326: the SP has switched off taking jobs itself.
SELF_NOT_TAKING_JOBS = "SELF_NOT_TAKING_JOBS"

# Admin timeline tie-break (SVC-333), see ``admin_timeline``. A Pro arriving
# (claimed / repicked) sorts after a status row stamped the same instant; any
# other kind is a Pro leaving, and sorts before.
_STATUS_RANK = 1
_ARRIVING_KINDS = frozenset({"claimed", "repicked"})


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
            .options(
                selectinload(BookingItem.service),
                # Eager-load the variant too so BookingItemResponse.from_item
                # can read ``item.service_variant.label`` without tripping
                # raise_on_sql. Nullable relationship — selectinload copes.
                selectinload(BookingItem.service_variant),
            )
            .where(BookingItem.booking_id.in_(booking_ids))
        )
        by_booking: dict[uuid.UUID, list[BookingItem]] = {}
        for item in rows.scalars():
            by_booking.setdefault(item.booking_id, []).append(item)
        for booking in bookings:
            set_committed_value(booking, "items", by_booking.get(booking.id, []))

    # ------------------------------------------------------------------
    # Status-history hydration
    # ------------------------------------------------------------------

    async def attach_professionals(self, bookings: Iterable[Booking]) -> None:
        """Load who is doing each booking, and for which business (SVC-324).

        Fourth of the read-path hydrators, for ``BookingResponse.professional``
        and ``.service_provider``. ``Booking.professional`` stays lazy for the
        Celery payee lookup, so an async path that skips this fails with
        MissingGreenlet. The explicit fetch (not ``selectinload``) is for the
        identity-map reason ``attach_addresses`` documents.

        The SP's profile isn't a relationship on ``Booking``: provider_profiles
        is keyed by ``user_id`` = ``booking.provider_id``. It is set as a plain,
        unmapped attribute, ``service_provider_profile``, which the unit of work
        never sees. Two queries regardless of page size.
        """
        bookings = list(bookings)
        if not bookings:
            return

        pro_ids = {b.professional_id for b in bookings if b.professional_id}
        pros: dict[uuid.UUID, Professional] = {}
        if pro_ids:
            rows = await self.db.execute(select(Professional).where(Professional.id.in_(pro_ids)))
            pros = {pro.id: pro for pro in rows.scalars()}

        sp_ids = {b.provider_id for b in bookings if b.provider_id}
        profiles: dict[uuid.UUID, ProviderProfile] = {}
        if sp_ids:
            rows = await self.db.execute(
                select(ProviderProfile).where(ProviderProfile.user_id.in_(sp_ids))
            )
            profiles = {profile.user_id: profile for profile in rows.scalars()}

        for booking in bookings:
            set_committed_value(booking, "professional", pros.get(booking.professional_id))
            booking.service_provider_profile = profiles.get(booking.provider_id)

    async def attach_status_logs(self, bookings: Iterable[Booking]) -> None:
        """Load each booking's transition history, ready for serialisation.

        Third of the read-path hydrators, and it exists for the reason the
        other two do: ``BookingResponse.status_history`` reads
        ``Booking.status_logs`` (SVC-248), that relationship is declared
        ``lazy="raise_on_sql"``, and on the list route an implicit load would
        be one query per row.

        The obvious alternative — ``selectinload(Booking.status_logs)`` on the
        query — is the trap ``attach_addresses`` documents. Every single-booking
        path here reaches the row through ``db.get``, which answers from the
        identity map when it can, and a load that emits no SQL applies no
        loader options. In production each request opens its own session, so a
        cold read does emit SQL and the option would appear to work; under the
        test suite, where ``get_db`` is overridden with one shared session, the
        fixture's booking is already in the map and the relationship silently
        stays unloaded. Passing in production and failing in CI is a worse
        failure than either.

        One query regardless of page size. Ordering comes from the
        relationship's own ``order_by`` rather than from this query, so a
        caller who loads the rows some other way still gets oldest-first;
        it is restated here because ``set_committed_value`` stores whatever
        sequence it is handed and will not re-sort it.

        ``set_committed_value`` marks the relationship *already loaded* rather
        than *changed*, which keeps this read-path hydration out of the unit of
        work — plain assignment would enrol every serialised booking in the
        next flush.

        Note for callers that have just written a transition: this emits a
        SELECT, and the session autoflushes before it, so a ``BookingStatusLog``
        added but not yet flushed is included. ``create_booking`` and
        ``transition_status`` both rely on that.
        """
        bookings = list(bookings)
        booking_ids = [b.id for b in bookings]
        if not booking_ids:
            return

        rows = await self.db.execute(
            select(BookingStatusLog)
            .where(BookingStatusLog.booking_id.in_(booking_ids))
            .order_by(BookingStatusLog.created_at)
        )
        by_booking: dict[uuid.UUID, list[BookingStatusLog]] = {}
        for log in rows.scalars():
            by_booking.setdefault(log.booking_id, []).append(log)
        for booking in bookings:
            set_committed_value(booking, "status_logs", by_booking.get(booking.id, []))

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
        rows = await self.db.execute(
            select(Service)
            .where(Service.id.in_(service_ids))
            .options(selectinload(Service.variants))
        )
        services_by_id = {s.id: s for s in rows.scalars()}

        for item in items:
            svc = services_by_id.get(item.service_id)
            if svc is None or not svc.is_active:
                # 400 rather than 404 — the caller sent a cart the platform
                # can't fulfil, which is a request problem, not a resource one.
                raise ValueError(f"Service {item.service_id} not found or inactive")

        # Second query for the explicitly-requested variants (if any), so
        # a caller who sent a stale variant_id fails cleanly rather than
        # silently falling back to the service's first variant.
        variant_ids = [i.service_variant_id for i in items if i.service_variant_id is not None]
        variants_by_id: dict[uuid.UUID, ServiceVariant] = {}
        if variant_ids:
            var_rows = await self.db.execute(
                select(ServiceVariant).where(ServiceVariant.id.in_(variant_ids))
            )
            variants_by_id = {v.id: v for v in var_rows.scalars()}
            for item in items:
                if item.service_variant_id is None:
                    continue
                v = variants_by_id.get(item.service_variant_id)
                if v is None or not v.is_active or v.service_id != item.service_id:
                    raise ValueError(
                        f"Variant {item.service_variant_id} not found, inactive, "
                        f"or does not belong to service {item.service_id}"
                    )

        # Legacy split (SVC-150) used for services that don't yet carry
        # variants — the pre-Pune-pricing rows. Once every seed row has a
        # variant this branch dies.
        commission_rate = to_decimal(settings.PLATFORM_COMMISSION_PERCENT) / 100

        # First pass: compute totals so we can seed the Booking row with the
        # aggregates. Second pass (after flush) creates the item rows keyed
        # to the booking id. Variant carries authoritative unit_price +
        # provider_payout snapshot; the split ratio isn't a global constant
        # any more (launch summary is 70/30, verticals 65/35), so we no
        # longer derive it from PLATFORM_COMMISSION_PERCENT when a variant
        # is in play.
        item_lines: list[
            tuple[
                CreateBookingItemRequest,
                Service,
                ServiceVariant | None,
                Decimal,
                Decimal,
                Decimal,
                Decimal,
            ]
        ] = []
        for item in items:
            svc = services_by_id[item.service_id]
            variant: ServiceVariant | None = None
            if item.service_variant_id is not None:
                variant = variants_by_id[item.service_variant_id]
            elif svc.variants:
                # Fallback: caller sent only service_id. Pick the first
                # active variant by sort_order. Documented as intentional
                # in the schema comment on CreateBookingItemRequest.
                variant = next((v for v in svc.variants if v.is_active), None)

            if variant is not None:
                unit_price = to_decimal(variant.base_price)
                unit_provider_payout = to_decimal(variant.provider_payout)
                unit_platform_commission = to_decimal(variant.platform_commission)
                total_price = to_decimal(unit_price * item.quantity)
                platform_commission = to_decimal(unit_platform_commission * item.quantity)
                provider_payout = to_decimal(unit_provider_payout * item.quantity)
            else:
                # Pre-variant service: fall back to services.base_price + the
                # config-driven split. Once the seed loads every service with
                # at least one variant this branch is dead code.
                unit_price = to_decimal(svc.base_price)
                total_price = to_decimal(unit_price * item.quantity)
                platform_commission = to_decimal(total_price * commission_rate)
                provider_payout = total_price - platform_commission

            item_lines.append(
                (
                    item,
                    svc,
                    variant,
                    unit_price,
                    total_price,
                    platform_commission,
                    provider_payout,
                )
            )

        # Line tuple indices (item, svc, variant, unit_price, total_price,
        # platform_commission, provider_payout) — the totals are the last
        # three, one shift right of the pre-variant tuple.
        total_service_amount = sum((line[4] for line in item_lines), start=Decimal(0))
        total_platform_commission = sum((line[5] for line in item_lines), start=Decimal(0))
        total_provider_payout = sum((line[6] for line in item_lines), start=Decimal(0))

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
        for (
            req,
            svc,
            variant,
            unit_price,
            total_price,
            platform_commission,
            provider_payout,
        ) in item_lines:
            item_row = BookingItem(
                booking_id=booking.id,
                service_id=svc.id,
                # None for pre-variant services; the fallback path in
                # create_booking above computes the split from base_price.
                # Once the seed loads every service with variants this
                # stays populated on every row.
                service_variant_id=variant.id if variant is not None else None,
                quantity=req.quantity,
                # unit_price comes from the variant when present, from
                # services.base_price otherwise. Either way it's snapshotted
                # here so a later price update on the catalogue does not
                # retro-alter the bill.
                unit_price=unit_price,
                total_price=total_price,
                platform_commission=platform_commission,
                provider_payout=provider_payout,
            )
            # Same reasoning as `items` below: populate the relationship
            # attributes manually so the response builder that runs after
            # this method doesn't fault on raise_on_sql.
            #
            # `service` is not optional here the way `service_variant` is —
            # BookingItemResponse.from_item reads `item.service.name` on every
            # item, so omitting it made *every* POST /bookings/ raise
            # InvalidRequestError and return a bare 500 (SVC-230). `svc` is
            # already loaded in this loop; nothing extra to fetch.
            set_committed_value(item_row, "service", svc)
            if variant is not None:
                set_committed_value(item_row, "service_variant", variant)
            else:
                set_committed_value(item_row, "service_variant", None)
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

        # After the transition is recorded, not before: the 201 carries
        # `status_history`, and this booking's first entry is the row written
        # immediately above. The SELECT autoflushes it into existence.
        await self.attach_status_logs([booking])
        await self.attach_professionals([booking])

        return booking

    # ------------------------------------------------------------------
    # Fetch single booking
    # ------------------------------------------------------------------

    async def is_assigned_pro(self, booking: Booking, user_id: uuid.UUID) -> bool:
        """True when ``user_id`` is the person this booking is assigned to: the
        user behind ``booking.professional_id``, while that Pro row is active
        (SVC-320 PR 3).

        A team Pro is a different user from the SP who holds the booking, and
        usually a consumer-role account (the app signs everyone up as one), so
        neither the provider check nor a role check covers them. Removing a Pro
        from the team (``is_active = false``) ends their access.
        """
        if booking.professional_id is None:
            return False
        pro = await self.db.get(Professional, booking.professional_id)
        return pro is not None and pro.is_active and pro.user_id == user_id

    async def get_booking(self, booking_id: uuid.UUID, user_id: uuid.UUID) -> Booking:
        """Fetch a booking for its consumer, its provider (the SP), or the Pro it
        is assigned to (SVC-320 PR 3)."""
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)
        if user_id not in (booking.consumer_id, booking.provider_id) and not (
            await self.is_assigned_pro(booking, user_id)
        ):
            raise UnauthorizedAction("You do not have access to this booking")
        # After the access check, not before: a caller who is refused never
        # causes a lookup of the address (or items) they were refused.
        await self.attach_addresses([booking])
        await self.attach_items([booking])
        await self.attach_status_logs([booking])
        await self.attach_professionals([booking])
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

        return await self._build_offer(booking, address, provider_id)

    async def _build_offer(
        self, booking: Booking, address: Address, provider_id: uuid.UUID
    ) -> BookingOfferResponse:
        """Render one offer for one provider. Assumes items are already attached
        and the caller has decided this provider may see it.

        Shared by ``get_offer`` (one booking, by id) and ``list_open_offers``
        (SVC-254's poll), so the pushed card and the polled card are built from
        exactly one code path and cannot drift apart.
        """
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

    async def list_open_offers(self, provider_id: uuid.UUID) -> list[BookingOfferResponse]:
        """Every offer this provider can still act on (SVC-254).

        The fallback for a provider whose FCM push never arrived — a stale
        token, a denied permission, an iOS Safari tab. Before this, a dropped
        push meant the job was simply invisible to them.

        Two sources feed the list:

        * **Rows written at dispatch.** The broadcast persists one per provider
          it pushed to, so an offer outlives the notification that announced it.
        * **A top-up.** Rows are written once, when the booking dispatches, so a
          provider who came online mid-window has none. Any still-open booking
          they are eligible for *now* gets a row here. This is the only place it
          can happen: that booking's push has already fired and will not be
          re-sent, so polling is the sole way they could ever learn of it.

        An offer once made stands until it is answered or the booking closes —
        losing eligibility afterwards (going offline, say) does not retract it.

        Sorted soonest-expiring first: the card shows one job at a time, and the
        one closest to dying is the one that needs an answer now.
        """
        # Gate on the provider-level half of the eligibility rule once, up front.
        #
        # Two things go wrong without it. A provider who has gone offline still
        # holds rows from when they were online, so they would be served cards
        # that ``accept_booking`` then refuses with 403 — it re-checks the same
        # predicate, ``is_online`` included. And a profile with no
        # ``service_area_center`` (nullable, and only *going online* requires it
        # since SVC-257) has no point to measure from, so ``_build_offer``'s
        # distance comes back NULL and raises — taking the whole list down with
        # it, not just that one offer. ``get_offer`` never hits either because it
        # re-runs the full eligibility check per booking; this path deliberately
        # does not, for rows that already exist.
        profile = (
            await self.db.execute(
                select(ProviderProfile).where(ProviderProfile.user_id == provider_id)
            )
        ).scalar_one_or_none()
        if (
            profile is None
            or not profile.is_online
            or profile.kyc_status is not KYCStatus.VERIFIED
            or profile.service_area_center is None
        ):
            return []

        # Genuinely open: REQUESTED, unclaimed, and the offer not yet expired.
        # The expiry check matters because `expire_stale_bookings` sweeps only
        # every 30s, so a booking can sit past its deadline still REQUESTED.
        # `offer_expires_at` covers the normal window (from creation or the
        # last reopen, SVC-320) and a paid booking's second offer (SVC-344).
        open_bookings = (
            (
                await self.db.execute(
                    select(Booking).where(
                        Booking.status == BookingStatus.REQUESTED,
                        Booking.provider_id.is_(None),
                        Booking.offer_expires_at > datetime.now(UTC),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not open_bookings:
            return []

        await self.attach_items(open_bookings)

        existing = (
            (
                await self.db.execute(
                    select(BookingOffer).where(
                        BookingOffer.provider_id == provider_id,
                        BookingOffer.booking_id.in_([b.id for b in open_bookings]),
                    )
                )
            )
            .scalars()
            .all()
        )
        rows_by_booking = {row.booking_id: row for row in existing}

        offers: list[BookingOfferResponse] = []
        top_ups: list[dict] = []

        for booking in open_bookings:
            row = rows_by_booking.get(booking.id)

            # Answered already — accepted elsewhere, or declined. Either way it
            # is not coming back.
            if row is not None and row.response is not None:
                continue

            address = await self.db.get(Address, booking.consumer_address_id)
            if address is None or address.location is None:
                # The broadcast bails on this same condition, so nobody was ever
                # offered this booking.
                continue

            if row is None:
                # Top-up path. Only include it if they qualify right now —
                # matching-policy (i), every service in the cart, same predicate
                # the broadcast used.
                eligible = await self.db.execute(
                    is_eligible_provider_multi_stmt(
                        [item.service_id for item in booking.items],
                        address.location,
                        provider_id,
                    )
                )
                if eligible.scalar_one_or_none() is None:
                    continue
                top_ups.append(
                    {
                        "id": uuid.uuid4(),
                        "booking_id": booking.id,
                        "provider_id": provider_id,
                    }
                )

            offers.append(await self._build_offer(booking, address, provider_id))

        if top_ups:
            # Racing polls from the same provider can both decide to top up the
            # same booking; the unique constraint settles it and neither fails.
            await self.db.execute(
                pg_insert(BookingOffer)
                .values(top_ups)
                .on_conflict_do_nothing(constraint="uix_booking_offer")
            )
            await self.db.commit()

        offers.sort(key=lambda offer: offer.seconds_remaining)
        return offers

    async def decline_offer(self, booking_id: uuid.UUID, provider_id: uuid.UUID) -> None:
        """Record that this provider does not want this job (SVC-254).

        Without it the poll would re-serve a dismissed offer every fifteen
        seconds for the life of the window, which is worse than showing nothing.

        403 when they were never offered it — same answer as ``get_offer`` gives
        a non-recipient, so declining cannot be used to probe which bookings
        exist. Declining an already-answered offer is a no-op rather than an
        error: the provider's intent is satisfied either way, and the common
        cause is a double tap.
        """
        row = (
            await self.db.execute(
                select(BookingOffer).where(
                    BookingOffer.booking_id == booking_id,
                    BookingOffer.provider_id == provider_id,
                )
            )
        ).scalar_one_or_none()

        if row is None:
            raise UnauthorizedAction("You were not offered this booking")

        if row.response is not None:
            return

        row.response = OfferResponse.DECLINED
        row.responded_at = datetime.now(UTC)
        await self.db.commit()

    @staticmethod
    def _offer_expiry(booking: Booking) -> tuple[datetime, int]:
        """When this offer dies, and how long the card has left (SVC-166).

        Read from ``Booking.offer_expires_at`` — the end of the normal window
        (creation or the last reopen, + the timeout), or of a paid booking's
        second offer (SVC-344) — the same definition ``expire_stale_bookings``
        sweeps on, so changing a timeout moves both the real expiry and what we
        advertise.

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
        expires_at = booking.offer_expires_at
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

        The precondition ladder lives in ``_check_claim``; see it for why the
        order is what it is.

        SVC-320 (two-level dispatch): accept is "assign the SP's own Pro". The
        claim also sets ``professional_id`` to the SP's Pro-for-self, so every
        accepted booking names the person doing it. That holds for a team SP
        too: only a client that opts in to the pick step (``team_pick_options``)
        gets the team list instead, and a team SP who accepted this way can hand
        the job to someone else with ``assign``. An old client therefore never
        mistakes "pick a Pro" for "you got it".
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)

        if await self._check_claim(booking, provider_id):
            # Idempotent retry: returns without reaching `transition_status`,
            # so this is the one accept path that has to hydrate for itself.
            await self.attach_addresses([booking])
            await self.attach_items([booking])
            await self.attach_status_logs([booking])
            await self.attach_professionals([booking])
            return booking, False

        self_pro = await ProviderService(self.db).ensure_self_professional(provider_id)
        # SVC-326: a plain accept claims with the Pro-for-self, so it can't go
        # through while the SP has stopped taking jobs. A 409, never the pick
        # list's 202: an old client reads any 2xx as "you got it".
        if self_pro is not None:
            self._refuse_if_not_taking_jobs(self_pro)
        booking = await self._claim(booking, provider_id, self_pro)
        return booking, True

    @staticmethod
    def _refuse_if_not_taking_jobs(pro: Professional) -> None:
        """409 when ``pro`` is a Pro-for-self that is switched off (SVC-326)."""
        if pro.is_self and not pro.accepts_direct_jobs:
            raise AppException(
                "You've switched off taking jobs yourself. "
                "Assign this booking to someone on your team.",
                status_code=status.HTTP_409_CONFLICT,
                code=SELF_NOT_TAKING_JOBS,
            )

    async def _check_claim(self, booking: Booking, provider_id: uuid.UUID) -> bool:
        """The accept precondition ladder, shared by ``accept_booking``,
        ``assign`` and ``team_pick_options`` so the three can't drift.

        Returns ``True`` when the caller already holds the booking at ACCEPTED
        (an idempotent retry, or a re-pick), ``False`` when it is theirs to
        claim. Raises for everything else.

        Order matters, and each branch is a case the card has to tell apart:

        * eligibility first (SVC-168), before revealing anything about the
          booking: 403 for a provider who was never offered it;
        * cancelled/expired next, so a provider re-accepting a booking they had
          claimed and which was later cancelled gets 410, not a stale 200;
        * then their own accepted booking → ``True``;
        * then anyone else's → 410, the lost race (SVC-167). Checked before the
          transition guard, which would otherwise always fire first and report
          the useless "cannot move from accepted to accepted".

        A provider whose booking has moved past ACCEPTED (en route, in progress)
        gets ``False`` and so reaches the transition guard, which answers 409:
        re-accepting there is a genuinely illegal move, not a retry.
        """
        # SVC-168: `get_offer` uses the same shared predicate
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
            return booking.status is BookingStatus.ACCEPTED
        if booking.provider_id is not None:
            # SVC-167: another provider won the race. From the card's point of
            # view this is the same outcome as cancelled/expired — the offer is
            # gone, dismiss it quietly. Was 403; the code drift with the
            # cancelled/expired path forced the frontend into
            # `if (403 || 410) dismiss()`. 410 unifies the vocabulary.
            raise BookingOfferClosed()
        return False

    async def _claim(self, booking: Booking, sp_id: uuid.UUID, pro: Professional | None) -> Booking:
        """REQUESTED → ACCEPTED for ``sp_id``, done by ``pro``. Caller holds the
        row lock and has run ``_check_claim``."""
        # SVC-254: stamp the winner's offer row. Queued *before* the transition
        # so ``transition_status``'s own commit carries both — the claim and the
        # record of who claimed it land atomically, and this path adds no second
        # commit of its own.
        #
        # Only the winner's row. The losers' are left untouched on purpose:
        # ``list_open_offers`` joins to the booking and filters on REQUESTED +
        # unclaimed, so a booking that has just been taken drops out of
        # everyone else's list with no write at all. That keeps a bulk UPDATE
        # off the accept path, and leaves their rows reading as what actually
        # happened — offered, never responded.
        await self.db.execute(
            update(BookingOffer)
            .where(
                BookingOffer.booking_id == booking.id,
                BookingOffer.provider_id == sp_id,
                BookingOffer.response.is_(None),
            )
            .values(response=OfferResponse.ACCEPTED, responded_at=datetime.now(UTC))
        )
        # Same row ``transition_status`` re-reads under its lock (identity map),
        # so this rides the transition's flush. ``provider_id`` is set there, to
        # ``changed_by`` — the SP, which is ``pro.service_provider_id``.
        booking.professional_id = pro.id if pro is not None else None
        booking = await self.transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.ACCEPTED,
            changed_by=sp_id,
        )
        if pro is not None:
            await self.record_assignment(booking, "claimed", None, pro.id, actor_id=sp_id)
        return booking

    # ------------------------------------------------------------------
    # Two-level dispatch (SVC-320 PR 2)
    # ------------------------------------------------------------------

    async def _active_pros(self, sp_id: uuid.UUID) -> list[Professional]:
        """The SP's active Pros, Pro-for-self first, then by join date."""
        result = await self.db.execute(
            select(Professional)
            .where(
                Professional.service_provider_id == sp_id,
                Professional.is_active.is_(True),
            )
            .order_by((Professional.user_id != sp_id), Professional.created_at, Professional.id)
        )
        return list(result.scalars().all())

    async def _busy_pro_ids(
        self, pro_ids: list[uuid.UUID], except_booking: uuid.UUID | None = None
    ) -> set[uuid.UUID]:
        """Which of these Pros already hold a job that isn't finished."""
        if not pro_ids:
            return set()
        stmt = select(Booking.professional_id).where(
            Booking.professional_id.in_(pro_ids),
            Booking.status.in_(ACTIVE_JOB_STATUSES),
        )
        if except_booking is not None:
            stmt = stmt.where(Booking.id != except_booking)
        return set((await self.db.execute(stmt)).scalars().all())

    async def team_pick_options(
        self, booking_id: uuid.UUID, sp_id: uuid.UUID
    ) -> list[tuple[Professional, bool]] | None:
        """For a client that opted in to the pick step: the Pros a team SP can
        assign this booking to, each with whether they're busy. No write.

        A switched-off Pro-for-self (SVC-326) stays in the list; the router
        flags it with its ``accepts_direct_jobs`` so the SP sees why.

        ``None`` means "not a pick": a solo SP, a booking they already hold, or
        one past REQUESTED. The router then runs the plain accept, so every
        outcome other than "here is your team" keeps accept's own answer.
        Eligibility and the closed/lost-race checks run first either way.
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)
        if await self._check_claim(booking, sp_id):
            return None
        if booking.status is not BookingStatus.REQUESTED:
            return None
        pros = await self._active_pros(sp_id)
        if not any(not pro.is_self for pro in pros):
            return None
        busy = await self._busy_pro_ids([pro.id for pro in pros])
        return [(pro, pro.id in busy) for pro in pros]

    async def assign(
        self, booking_id: uuid.UUID, sp_id: uuid.UUID, professional_id: uuid.UUID
    ) -> tuple[Booking, bool, Professional | None]:
        """The SP picks the Pro who does this job: the atomic claim of two-level
        dispatch, first write wins. Returns ``(booking, newly_claimed,
        newly_assigned_pro)``; the last is ``None`` when nothing changed.

        Two cases:

        * **Claim.** The booking is REQUESTED and unclaimed: same preconditions,
          same row lock, same offer stamp and the same ACCEPTED transition as
          ``accept_booking``, with the picked Pro instead of the Pro-for-self.
          Two SPs racing get one 200 and one 410.
        * **Re-pick.** The caller already holds it at ACCEPTED: set or replace
          its Pro (after a decline, or an accept that defaulted to the
          Pro-for-self). No status change. Assigning the Pro already on it is
          an idempotent 200 with no push.

        The Pro must be one of the caller's active Pros (404 otherwise, the
        same for "not yours" and "doesn't exist"), and not busy on another
        unfinished job (409 ``PRO_BUSY``). That check is ``assign``'s alone:
        the solo accept path never had it, and adding it there would change
        live behaviour. A team Pro must also have verified KYC (409
        ``PRO_KYC_INCOMPLETE``) and be online (409 ``PRO_OFFLINE``); the
        Pro-for-self is exempt from both, since the SP's own KYC and online
        state are part of eligibility; instead it is refused while the SP has
        switched off taking jobs itself (409 ``SELF_NOT_TAKING_JOBS``, SVC-326).
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)

        holds_it = await self._check_claim(booking, sp_id)

        pro = await self.db.get(Professional, professional_id)
        if pro is None or pro.service_provider_id != sp_id or not pro.is_active:
            raise AppException(
                "That professional isn't on your team.",
                status_code=status.HTTP_404_NOT_FOUND,
                code=PRO_NOT_FOUND,
            )

        if holds_it and booking.professional_id == pro.id:
            await self.attach_addresses([booking])
            await self.attach_items([booking])
            await self.attach_status_logs([booking])
            await self.attach_professionals([booking])
            return booking, False, None

        if not holds_it and booking.status is not BookingStatus.REQUESTED:
            # Theirs, but past ACCEPTED: en route or later.
            raise InvalidStateTransition(
                f"A booking that is {booking.status.value} can't be reassigned"
            )

        self._refuse_if_not_taking_jobs(pro)

        # A team Pro goes into a consumer's home, so their own identity KYC
        # must be approved first (SVC-318; wireframes §3B). Nothing else stops
        # it: accept-invite and /pros/me/online have no KYC gate, and
        # broadcast eligibility checks only the SP's KYC. The Pro-for-self is
        # exempt: its KYC *is* the SP's, which eligibility already requires.
        if not pro.is_self and pro.kyc_status != KYCStatus.VERIFIED:
            raise AppException(
                f"{pro.display_name} hasn't finished KYC yet.",
                status_code=status.HTTP_409_CONFLICT,
                code=PRO_KYC_INCOMPLETE,
            )

        # SVC-320 PR 3: team Pros now have their own online toggle. The SP's
        # Pro-for-self is exempt: the SP's toggle, part of eligibility, covers it.
        if not pro.is_self and not pro.is_online:
            raise AppException(
                f"{pro.display_name} is offline.",
                status_code=status.HTTP_409_CONFLICT,
                code=PRO_OFFLINE,
            )

        if await self._busy_pro_ids([pro.id], except_booking=booking.id):
            raise AppException(
                f"{pro.display_name} is already on another job.",
                status_code=status.HTTP_409_CONFLICT,
                code=PRO_BUSY,
            )

        if holds_it:
            await self.record_assignment(
                booking, "repicked", booking.professional_id, pro.id, actor_id=sp_id
            )
            booking.professional_id = pro.id
            await self.db.flush()
            await self.attach_addresses([booking])
            await self.attach_items([booking])
            await self.attach_status_logs([booking])
            await self.attach_professionals([booking])
            logger.info("booking.repicked booking_id=%s pro_id=%s", booking.id, pro.id)
            return booking, False, pro

        booking = await self._claim(booking, sp_id, pro)
        return booking, True, pro

    async def decline_job(self, booking_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Booking, str]:
        """The assigned Pro turns the job down. Returns ``(booking, outcome)``.

        Authorized by the Pro row, not a role: the caller must have an *active*
        ``professionals`` row that is this booking's Pro. Invitees sign up as
        consumers, so a role check would lock them out. Anyone else gets the
        same 404 as for a booking that doesn't exist.

        Only while ACCEPTED (assigned, not yet set off); later is a 409.

        * ``"awaiting_repick"`` — the SP has another active Pro. The booking
          stays theirs at ACCEPTED with no Pro, and the router pushes the SP to
          re-pick.
        * ``"reopened"`` — nobody else on the team (a solo SP declining their
          own job included): back to REQUESTED and re-broadcast.
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        pro = (
            await self.db.get(Professional, booking.professional_id)
            if booking is not None and booking.professional_id is not None
            else None
        )
        if pro is None or pro.user_id != user_id or not pro.is_active:
            raise AppException(
                "Job not found.", status_code=status.HTTP_404_NOT_FOUND, code=JOB_NOT_FOUND
            )
        if booking.status is not BookingStatus.ACCEPTED:
            raise InvalidStateTransition(f"A job that is {booking.status.value} can't be declined")

        others = [p for p in await self._active_pros(booking.provider_id) if p.id != pro.id]
        await self.record_assignment(booking, "declined", pro.id, None, actor_id=user_id)
        booking.professional_id = None
        if others:
            await self.db.flush()
            await self.attach_addresses([booking])
            await self.attach_items([booking])
            await self.attach_status_logs([booking])
            await self.attach_professionals([booking])
            logger.info("booking.pro_declined booking_id=%s pro_id=%s", booking.id, pro.id)
            return booking, "awaiting_repick"

        booking = await self.reopen(booking, changed_by=user_id, reason="pro_declined")
        return booking, "reopened"

    async def release(self, booking_id: uuid.UUID, sp_id: uuid.UUID) -> Booking:
        """The SP gives an accepted booking back to the broadcast (SVC-320).

        The SP's way out when nobody on the team can take the job. Allowed
        while ACCEPTED, whether or not a Pro is assigned; once someone has set
        off it's a 409. A booking the caller doesn't hold is a 403, as on
        every other per-booking provider action.
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)
        if booking.provider_id != sp_id:
            raise UnauthorizedAction("You do not hold this booking")
        if booking.status is not BookingStatus.ACCEPTED:
            raise InvalidStateTransition(
                f"A booking that is {booking.status.value} can't be released"
            )
        # Before ``reopen``, which clears the Pro.
        await self.record_assignment(
            booking, "released", booking.professional_id, None, actor_id=sp_id
        )
        return await self.reopen(booking, changed_by=sp_id, reason="released")

    async def reopen(self, booking: Booking, changed_by: uuid.UUID, reason: str) -> Booking:
        """ACCEPTED → REQUESTED: unclaim the booking so it can be broadcast again.

        Its own method rather than a ``transition_status`` call, because the
        actor may be a team Pro, who is neither the booking's provider nor its
        consumer and would trip that method's ownership guard. Callers hold the
        row lock and have checked who may do this.

        * ``provider_id`` and ``professional_id`` are cleared, so the booking is
          open to every eligible SP again.
        * ``reopened_at`` restarts the 2-minute offer window; without it the
          next expiry sweep would kill a booking that reopened minutes after it
          was created.
        * The releasing SP's offer row is marked answered (it normally already
          is, ACCEPTED, from the claim), so the re-broadcast and the offer poll
          leave them out. They gave it back; they shouldn't be offered it again.
        * A new doorstep OTP (SVC-344): the consumer has already read the old
          one out, or could have, to someone who is no longer on the job.
        * ``last_chance_at`` is cleared, so a paid booking starts its no-taker
          cycle afresh: a full window, then the second offer (SVC-344).

        The router enqueues the consumer push and the re-broadcast after commit.
        """
        allowed = VALID_TRANSITIONS.get(booking.status, [])
        if BookingStatus.REQUESTED not in allowed:
            raise InvalidStateTransition(
                f"Cannot move from {booking.status.value} to {BookingStatus.REQUESTED.value}."
            )
        old_status = booking.status
        releasing_sp = booking.provider_id
        now = datetime.now(UTC)

        if releasing_sp is not None:
            await self.db.execute(
                pg_insert(BookingOffer)
                .values(
                    id=uuid.uuid4(),
                    booking_id=booking.id,
                    provider_id=releasing_sp,
                    response=OfferResponse.DECLINED,
                    responded_at=now,
                )
                .on_conflict_do_nothing(constraint="uix_booking_offer")
            )

        booking.status = BookingStatus.REQUESTED
        booking.provider_id = None
        booking.professional_id = None
        booking.reopened_at = now
        booking.last_chance_at = None
        booking.doorstep_otp = self._generate_doorstep_otp()
        await self._log_transition(
            booking_id=booking.id,
            from_status=old_status.value,
            to_status=BookingStatus.REQUESTED.value,
            changed_by=changed_by,
            metadata={
                "reason": reason,
                "released_by_provider": str(releasing_sp) if releasing_sp else None,
            },
        )
        await self.db.flush()
        await self.attach_addresses([booking])
        await self.attach_items([booking])
        await self.attach_status_logs([booking])
        await self.attach_professionals([booking])
        logger.info(
            "booking.reopened booking_id=%s reason=%s released_by=%s",
            booking.id,
            reason,
            releasing_sp,
        )
        return booking

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
        elif new_status in (
            BookingStatus.EN_ROUTE,
            BookingStatus.IN_PROGRESS,
            BookingStatus.COMPLETED,
        ):
            # The job's own steps: only whoever is doing it — the SP holding the
            # booking, or the Pro it is assigned to (SVC-320 PR 3). Never the
            # consumer: before PR 3 this branch admitted them too, so a consumer
            # could mark their own booking complete, crediting the provider's
            # payout and firing the cash-payment task. The app never let them,
            # but the API did.
            if changed_by != booking.provider_id and not (
                await self.is_assigned_pro(booking, changed_by)
            ):
                raise UnauthorizedAction("Only the assigned provider can perform this action")
        elif changed_by not in (booking.provider_id, booking.consumer_id):
            # Cancel: either party, as always (who pays what is
            # _apply_cancellation_policy's business, and cancel() handles a
            # provider backing out of an ACCEPTED job before this).
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
        # renders `address` (SVC-170), `items`, and `status_history` (SVC-248).
        # The history hydrator runs last and deliberately after
        # `_log_transition` above: the entry for the transition just made is
        # part of the response, and the SELECT autoflushes the pending row.
        await self.attach_addresses([booking])
        await self.attach_items([booking])
        await self.attach_status_logs([booking])
        await self.attach_professionals([booking])

        return booking

    # ------------------------------------------------------------------
    # Cancel, and paid bookings nobody takes (SVC-344)
    # ------------------------------------------------------------------

    async def cancel(
        self, booking_id: uuid.UUID, user_id: uuid.UUID, reason: str | None
    ) -> tuple[Booking, str]:
        """``PATCH /bookings/{id}/cancel``. Returns ``(booking, outcome)`` where
        outcome is ``"cancelled"`` or ``"reopened"``.

        The consumer cancels exactly as before, in every state, fee included.

        The booking's provider backing out is different (SVC-344):

        * while ACCEPTED the booking is **reopened**, not cancelled: back to
          searching, other SPs get it, nobody pays a fee. Before this, a
          provider cancel went through ``_apply_cancellation_policy``, whose fee
          depends only on the old status — so the *customer* was charged the
          ₹49 and it was credited to the provider who backed out;
        * once EN_ROUTE it's refused: they're on their way, so it needs a
          person (support), not an automatic reopen.

        Anyone else falls through to ``transition_status``, which refuses them
        as it always has.
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)
        if booking.provider_id == user_id and booking.consumer_id != user_id:
            if booking.status is BookingStatus.ACCEPTED:
                # Before ``reopen``, which clears the Pro.
                await self.record_assignment(
                    booking, "provider_cancelled", booking.professional_id, None, actor_id=user_id
                )
                booking = await self.reopen(
                    booking, changed_by=user_id, reason="provider_cancelled"
                )
                return booking, "reopened"
            if booking.status is BookingStatus.EN_ROUTE:
                raise AppException(
                    "You're on the way — contact support to cancel this job.",
                    status_code=status.HTTP_409_CONFLICT,
                    code=PROVIDER_EN_ROUTE,
                )
        booking = await self.transition_status(
            booking_id=booking_id,
            new_status=BookingStatus.CANCELLED,
            changed_by=user_id,
            cancellation_reason=reason,
        )
        return booking, "cancelled"

    async def advance_paid_unclaimed(self, booking_id: uuid.UUID) -> str | None:
        """Move a PAID booking whose offer expired untaken one step (SVC-344).

        Called by the sweep, once per due booking, in its own transaction:

        * no second offer yet → start one: ``last_chance_at = now`` (the offer
          now expires BOOKING_LAST_CHANCE_SECONDS later) → ``"second_offer"``;
          the sweep then re-broadcasts, to the same recipients rule as a reopen
          (eligible, minus SPs who already answered);
        * second offer over too → the system cancels it with a full refund →
          ``"refunded"``; the sweep then pushes the consumer.

        Everything is re-checked under the row lock, so a double sweep, a Celery
        retry, or someone accepting in between makes this a no-op (``None``).
        Scheduled bookings are left to their slot (SVC-243), as in the sweep.
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        now = datetime.now(UTC)
        if (
            booking is None
            or booking.status is not BookingStatus.REQUESTED
            or booking.provider_id is not None
            or booking.payment_status is not PaymentStatus.PAID
            or booking.payment_method is not PaymentMethod.ONLINE
            or (booking.scheduled_at is not None and booking.scheduled_at > now)
            or booking.offer_expires_at >= now
        ):
            return None

        if booking.last_chance_at is None:
            booking.last_chance_at = now
            await self.db.flush()
            logger.info("booking.second_offer booking_id=%s", booking.id)
            return "second_offer"

        await self._system_cancel_no_taker(booking)
        return "refunded"

    async def _system_cancel_no_taker(self, booking: Booking) -> None:
        """REQUESTED → CANCELLED by the system, refunding everything the
        customer paid.

        Reuses ``_apply_cancellation_policy``: from REQUESTED it refunds the full
        captured amount (service − discount) with no fee, and ``initiate_refund``
        is idempotent on the payment row. If Razorpay refuses, that raises and
        the caller's transaction rolls this whole cancel back: the booking stays
        REQUESTED + PAID for the next sweep, never "cancelled" without a refund.
        """
        old_status = booking.status
        booking.status = BookingStatus.CANCELLED
        booking.cancellation_reason = "No professional available"
        booking.doorstep_otp = None
        await self._apply_cancellation_policy(booking, old_status)
        await self._log_transition(
            booking_id=booking.id,
            from_status=old_status.value,
            to_status=BookingStatus.CANCELLED.value,
            # NULL: the system, not a person.
            changed_by=None,
            metadata={"actor": "system", "reason": "no_taker"},
            note="system: no professional available",
        )
        await self.db.flush()
        logger.info(
            "booking.no_taker_refunded booking_id=%s reopened=%s",
            booking.id,
            booking.reopened_at is not None,
        )

    # ------------------------------------------------------------------
    # Admin force-cancel (SVC-299)
    # ------------------------------------------------------------------

    async def force_cancel(
        self,
        booking_id: uuid.UUID,
        admin_id: uuid.UUID,
        reason: str,
    ) -> Booking:
        """Admin-driven cancel on a customer's behalf (SVC-299).

        Bypasses the customer/provider-only guard in ``transition_status``
        and the ``VALID_TRANSITIONS`` gate — an admin can cancel from any of
        REQUESTED / ACCEPTED / EN_ROUTE / IN_PROGRESS, states the state
        machine otherwise forbids each other's direction on. Refuses when
        the booking is already terminal (COMPLETED / CANCELLED / EXPIRED):
        409 with a clear message rather than a silent no-op, so support
        knows the row they're looking at is already closed.

        Refund policy is inherited from ``_apply_cancellation_policy`` and
        deliberately not reimplemented — the "captured PAID online payment
        gets refunded, pending/cash do not, coupon-heavy bookings cap the
        fee at what we hold" rules are already the ones support wants for
        an admin cancel. The IN_PROGRESS branch there returns without a
        refund on purpose (the job has already started, so an auto-refund
        is not the safe default) — an admin who needs to refund a live
        booking uses the payments surface.

        The transition is recorded in ``booking_status_logs`` with the
        admin as ``changed_by`` and the reason in the new ``note`` column,
        so the audit trail shows both who acted and why. The reason is
        also stamped on ``booking.cancellation_reason`` — the ticket asked
        for both, and they answer different questions: the log is the
        audit story, the column is the row's current explanation.
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)

        if booking.status in (
            BookingStatus.COMPLETED,
            BookingStatus.CANCELLED,
            BookingStatus.EXPIRED,
        ):
            raise InvalidStateTransition(
                f"Cannot force-cancel a booking that is already {booking.status.value}."
            )

        old_status = booking.status
        booking.status = BookingStatus.CANCELLED
        booking.cancellation_reason = reason
        # Same OTP-wipe rule ``transition_status`` applies on CANCELLED — the
        # PIN is meaningless once the booking is closed, and leaving a
        # plaintext secret live on a long-lived row is the failure mode
        # SVC-169 called out.
        booking.doorstep_otp = None

        # Reuse — not reimplement — the consumer cancel refund path (SVC-178).
        # Ordering matches ``transition_status``: refund initiation runs
        # before we log the transition, so a Razorpay failure rolls the whole
        # state change back atomically instead of leaving a "cancelled but no
        # refund started" row.
        await self._apply_cancellation_policy(booking, old_status)

        await self._log_transition(
            booking_id=booking.id,
            from_status=old_status.value,
            to_status=BookingStatus.CANCELLED.value,
            changed_by=admin_id,
            metadata={"admin_override": True},
            note=reason,
        )

        await self.attach_addresses([booking])
        await self.attach_items([booking])
        await self.attach_status_logs([booking])
        await self.attach_professionals([booking])
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

        # Newest first, and ordered at all — this query had no ORDER BY.
        #
        # Two things were wrong with that. The visible one: rows came back in
        # whatever order Postgres happened to hold them, which is roughly
        # insertion order until a row is UPDATEd, at which point the new
        # version lands elsewhere. A booking the user had just cancelled
        # therefore sank to the bottom of "Past" instead of rising to the top,
        # and read as missing.
        #
        # The quiet one: LIMIT/OFFSET over an unordered query has no defined
        # result. Postgres is free to return a row on two pages or on neither,
        # so paging past the first 20 could silently drop bookings.
        #
        # `id` breaks ties. `created_at` is `server_default=func.now()`, which
        # in Postgres is transaction start time, so two bookings created in one
        # transaction share a timestamp exactly — and a tie under LIMIT/OFFSET
        # reintroduces the instability this line exists to remove.
        stmt = stmt.order_by(Booking.created_at.desc(), Booking.id.desc())
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
        await self.attach_status_logs(bookings)
        await self.attach_professionals(bookings)

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
        await self.attach_status_logs([booking])
        await self.attach_professionals([booking])
        return booking

    # ------------------------------------------------------------------
    # Admin timeline (SVC-333)
    # ------------------------------------------------------------------

    async def admin_timeline(self, booking_id: uuid.UUID) -> list[BookingTimelineItem]:
        """Status changes and assignment events for one booking, as one list,
        oldest first. Admin only: it names Pros and actors.

        Rows written in the same transaction share ``created_at`` (``now()`` is
        the transaction's start time), so a plain sort would put a claim's
        "claimed" before its own requested → accepted, half the time. Ties
        break by what happened first in the code: the leaving event (declined,
        released, provider_cancelled) precedes the accepted → requested reopen
        it causes, and the status change precedes the claim or re-pick that
        follows it. ``id`` makes the order total.
        """
        if await self.db.get(Booking, booking_id) is None:
            raise BookingNotFound(booking_id)

        logs = (
            await self.db.scalars(
                select(BookingStatusLog).where(BookingStatusLog.booking_id == booking_id)
            )
        ).all()
        events = (
            await self.db.scalars(
                select(BookingAssignmentEvent).where(
                    BookingAssignmentEvent.booking_id == booking_id
                )
            )
        ).all()

        pro_ids = {
            pid
            for e in events
            for pid in (e.from_professional_id, e.to_professional_id)
            if pid is not None
        }
        pros: dict[uuid.UUID, Professional] = {}
        if pro_ids:
            rows = await self.db.execute(select(Professional).where(Professional.id.in_(pro_ids)))
            pros = {p.id: p for p in rows.scalars()}
        actor_ids = {log.changed_by for log in logs} | {e.actor_user_id for e in events}
        actor_ids.discard(None)
        names: dict[uuid.UUID, str | None] = {}
        if actor_ids:
            rows = await self.db.execute(select(User.id, User.name).where(User.id.in_(actor_ids)))
            names = {uid: name for uid, name in rows.all()}

        def pro(pid: uuid.UUID | None) -> TimelinePro | None:
            p = pros.get(pid) if pid is not None else None
            return TimelinePro(id=p.id, display_name=p.display_name) if p else None

        keyed: list[tuple[tuple, BookingTimelineItem]] = []
        for log in logs:
            item = StatusTimelineItem(
                type="status",
                created_at=log.created_at,
                from_status=log.from_status,
                to_status=log.to_status,
                actor_user_id=log.changed_by,
                actor_name=names.get(log.changed_by),
                note=log.note,
            )
            keyed.append(((log.created_at, _STATUS_RANK, str(log.id)), item))
        for e in events:
            item = AssignmentTimelineItem(
                type="assignment",
                created_at=e.created_at,
                kind=e.kind,
                from_professional=pro(e.from_professional_id),
                to_professional=pro(e.to_professional_id),
                actor_user_id=e.actor_user_id,
                actor_name=names.get(e.actor_user_id),
                note=e.note,
            )
            rank = _STATUS_RANK + (1 if e.kind in _ARRIVING_KINDS else -1)
            keyed.append(((e.created_at, rank, str(e.id)), item))
        keyed.sort(key=lambda pair: pair[0])
        return [item for _, item in keyed]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _log_transition(
        self,
        booking_id: uuid.UUID,
        from_status: str,
        to_status: str,
        changed_by: uuid.UUID | None,
        metadata: dict | None = None,
        note: str | None = None,
    ) -> None:
        log = BookingStatusLog(
            booking_id=booking_id,
            from_status=from_status,
            to_status=to_status,
            changed_by=changed_by,
            metadata_=metadata or {},
            note=note,
        )
        self.db.add(log)

    async def record_assignment(
        self,
        booking: Booking,
        kind: str,
        from_pro_id: uuid.UUID | None,
        to_pro_id: uuid.UUID | None,
        actor_id: uuid.UUID | None,
        note: str | None = None,
    ) -> None:
        """Record a change of the booking's Pro (SVC-333). The only writer of
        ``booking_assignment_events``: every assignment path goes through here.

        Only adds the row to the session, so it commits or rolls back with the
        change it describes: an assign that fails afterwards leaves no row.
        Callers record only an actual change, never a same-Pro re-assign.
        ``actor_id`` None means the system.
        """
        self.db.add(
            BookingAssignmentEvent(
                booking_id=booking.id,
                kind=kind,
                from_professional_id=from_pro_id,
                to_professional_id=to_pro_id,
                actor_user_id=actor_id,
                note=note,
            )
        )

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
