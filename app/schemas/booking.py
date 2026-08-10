from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, model_validator

if TYPE_CHECKING:
    from app.models.booking import Booking


class CreateBookingItemRequest(BaseModel):
    """One line item on a booking-creation request."""

    service_id: UUID
    # Same-service duplicates express "two of this" (e.g. two haircuts). At
    # the DB level a booking can't have two rows with the same service_id
    # (uix_booking_item_service), so the API forces the caller to consolidate
    # here rather than silently deduping later and losing money on the second
    # copy.
    quantity: int = Field(default=1, ge=1, le=10)


class CreateBookingRequest(BaseModel):
    """Create a booking as either a multi-item cart (``items``) or a single
    service (``service_id``).

    ``service_id`` is the legacy single-service shape retained for the mobile
    app while the cart UI is rolling out (PR 2c). It is normalised to a
    1-item ``items`` list inside the validator so the service layer sees one
    shape. Once the cart UI has shipped everywhere the ``service_id`` field
    can be removed and callers forced onto ``items``.
    """

    # Nullable + validator-checked rather than a discriminated union because
    # each shape carries the SAME other fields (address, scheduled_at, payment
    # method) — using a Union would push those into two duplicated schemas
    # and, more importantly, wouldn't help the mobile clients that are already
    # sending the flat legacy shape.
    items: list[CreateBookingItemRequest] | None = None
    service_id: UUID | None = None
    address_id: UUID
    scheduled_at: datetime | None = None  # None = instant booking
    payment_method: str = Field(default="online", pattern=r"^(cash|online)$")

    @model_validator(mode="after")
    def _normalise_items(self) -> "CreateBookingRequest":
        """Force one canonical shape (``items``) after validation.

        The service layer only ever sees ``self.items``; ``service_id`` is a
        wire-format convenience, not a domain concept.
        """
        if self.items and self.service_id:
            raise ValueError("Pass either 'items' or 'service_id', not both")
        if not self.items and not self.service_id:
            raise ValueError("Booking must have at least one service")
        if self.service_id and not self.items:
            self.items = [CreateBookingItemRequest(service_id=self.service_id, quantity=1)]
        return self


class BookingItemResponse(BaseModel):
    """One line on a booking response.

    ``service_name`` is denormalised into the response so the mobile app
    doesn't have to cross-reference against /services for every booking it
    displays — the list route already returns dozens of items and doing that
    lookup client-side is exactly the sort of N+1 pattern the frontend keeps
    hitting. Read from the loaded ``BookingItem.service`` relationship, which
    must be eager-loaded (see ``BookingItem.service`` — ``raise_on_sql``).
    """

    id: UUID
    service_id: UUID
    service_name: str
    quantity: int
    unit_price: float
    total_price: float

    model_config = {"from_attributes": True}

    @classmethod
    def from_item(cls, item) -> "BookingItemResponse":
        """Adapter that reads ``service.name`` off the loaded relationship.

        Written as a classmethod (rather than a plain ``model_validate``) so
        the pydantic model itself doesn't need to know about the ORM
        relationship — keeping schemas free of ORM lookups keeps them
        serialisable in isolation and testable without a session.
        """
        return cls(
            id=item.id,
            service_id=item.service_id,
            service_name=item.service.name,
            quantity=item.quantity,
            unit_price=item.unit_price,
            total_price=item.total_price,
        )


class BookingAddress(BaseModel):
    """Where the job is, as the booking's two parties see it (SVC-170).

    Its own schema rather than a reuse of ``AddressResponse``, which backs the
    consumer's own address book: that one carries ``is_default``, a detail of how
    the consumer manages their saved addresses and nothing a provider standing
    outside the building has any use for. Same principle that gave
    ``BookingOfferResponse`` its own shape (SVC-153) — serve each audience the
    fields its screen actually renders.

    ``lat``/``lng`` are the point of this being a nested object rather than a bare
    ``full_address`` string. The job to be done is *navigate there*: with
    coordinates the app can hand off to the device maps app, without them the
    provider retypes a free-text Indian address into Google Maps and trusts its
    geocoder — the failure mode this ticket exists to remove. ``get_offer``
    already reads the same geography to derive ``distance_km``.

    Both are optional because ``location`` is nullable: ``CreateAddressRequest``
    accepts an address with no coordinates, and older rows have none. Clients must
    treat text as the source of truth and coordinates as the enhancement, never
    the reverse.
    """

    id: UUID
    label: str
    full_address: str
    lat: float | None
    lng: float | None

    model_config = {"from_attributes": True}


class BookingResponse(BaseModel):
    id: UUID
    consumer_id: UUID
    provider_id: UUID | None
    # `items` replaces the old single `service_id` field — a booking is now
    # a cart. Required and non-defaulted so it stays in OpenAPI `required`
    # and generated clients don't type it optional (same reason
    # `BookingOfferResponse` fields have no defaults).
    items: list[BookingItemResponse]
    status: str
    # Sum of items' total_price / platform_commission / provider_payout at
    # booking time. Snapshotted on Booking; see the model's field comment.
    service_amount: float
    platform_commission: float
    provider_payout: float
    payment_method: str | None
    payment_status: str
    # Only visible to the consumer — and, unlike before SVC-169, actually
    # enforced. Build this schema with `for_viewer`, never `model_validate`,
    # on any path that can be reached by someone other than the consumer.
    doorstep_otp: str | None = None
    # Where the job is (SVC-170). Visible to the booking's two parties and nobody
    # else — see `for_viewer`. Read from the `consumer_address` relationship,
    # which must already be loaded: it is declared `lazy="raise_on_sql"`, so a
    # caller who forgets to eager-load gets a loud error here rather than an
    # implicit query (which under AsyncSession is a MissingGreenlet, and on the
    # list route would be one query per row).
    #
    # Nullable but *required* — no default, so it stays in the OpenAPI `required`
    # set and the generated client types it `BookingAddress | null` rather than
    # optional. `null` is a real answer here (admin listings), and a client that
    # must handle null is a client that will not silently render a blank card.
    address: BookingAddress | None = Field(
        validation_alias=AliasChoices("address", "consumer_address")
    )
    scheduled_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def for_viewer(cls, booking: "Booking", viewer_id: UUID | None) -> "BookingResponse":
        """Serialise a booking for one caller, blanking what they may not see.

        Two fields are gated, by two different rules, and the difference is the
        whole point of them being separate rules:

        ``address`` goes to the booking's **two parties** — the consumer, and the
        provider once assigned. The provider needs it precisely because they have
        accepted: the job is to travel to that door (SVC-170). Before assignment
        they get the pre-accept subset via ``BookingOfferResponse`` instead, and
        anyone who is not a party gets ``None``. That last case is unreachable
        through the routes today — ``get_booking`` raises ``UnauthorizedAction``
        for a non-party and ``list_bookings`` filters to the caller's own rows —
        and it is enforced here anyway, so the guarantee survives a future route
        that forgets. Deliberately *not* keyed on role: what matters is whether
        this caller is on this booking.

        ``viewer_id=None`` (the admin listing) gets no address. Not because an
        address is a secret an admin may not hold — an admin can already reach it
        in the addresses table — but because ``GET /admin/bookings`` is a bulk
        listing of up to 200 rows across every consumer on the platform, and a
        page of home addresses is a wholesale PII surface that no admin screen
        needs in order to show a table of bookings. The targeted form of the same
        question is ``GET /admin/bookings/{id}``, which does answer it — see
        ``for_admin``.

        ``doorstep_otp`` gates en_route → in_progress, so it has to reach the
        provider from the consumer at the door rather than from this API — the
        rule ``BookingOfferResponse`` already documents (SVC-153) and which
        ``BookingResponse`` was declaring in a comment but never applying
        (SVC-169). Serving it to the assigned provider made the doorstep check
        ceremonial: they could read their own booking and skip the door.

        Anyone who is not the consumer gets ``None``, including a ``viewer_id``
        of ``None`` (admin listings, which have no booking-party identity).

        The blanking happens on this validated *copy*, deliberately. The
        assignment must never be made against the ORM object on a read path::

            booking = await self.get_booking(booking_id, user_id)
            booking.doorstep_otp = None   # persists — do not do this

        ``get_booking`` returns a session-attached ``Booking`` and ``get_db``
        commits at the end of every successful request (``app/database.py:65``),
        so that would erase the PIN from the row instead of hiding it from the
        response, and ``/start`` would then have nothing left to match. The
        column is nullable since PR #59, so no IntegrityError would catch it —
        the read would quietly wipe the row and return 200. The one place that
        assignment belongs is ``transition_status``, which wipes the OTP on
        IN_PROGRESS / COMPLETED / CANCELLED because it is spent by then.
        """
        # ``items`` needs each element flattened with the loaded
        # ``service.name`` — a two-hop lookup that pydantic's default
        # ``from_attributes`` traversal doesn't do. Build the item list
        # explicitly, and hand pydantic a dict for everything else so it
        # never tries to auto-convert booking.items and fail on the missing
        # ``service_name`` attribute.
        response = cls(
            id=booking.id,
            consumer_id=booking.consumer_id,
            provider_id=booking.provider_id,
            items=[BookingItemResponse.from_item(i) for i in booking.items],
            status=booking.status.value if hasattr(booking.status, "value") else booking.status,
            service_amount=booking.service_amount,
            platform_commission=booking.platform_commission,
            provider_payout=booking.provider_payout,
            payment_method=(
                booking.payment_method.value
                if hasattr(booking.payment_method, "value")
                else booking.payment_method
            ),
            payment_status=(
                booking.payment_status.value
                if hasattr(booking.payment_status, "value")
                else booking.payment_status
            ),
            doorstep_otp=booking.doorstep_otp,
            address=(
                BookingAddress.model_validate(booking.consumer_address)
                if booking.consumer_address is not None
                else None
            ),
            scheduled_at=booking.scheduled_at,
            created_at=booking.created_at,
        )
        if viewer_id is None or viewer_id != booking.consumer_id:
            response.doorstep_otp = None
        if viewer_id is None or viewer_id not in (booking.consumer_id, booking.provider_id):
            response.address = None
        return response

    @classmethod
    def for_admin(cls, booking: "Booking") -> "BookingResponse":
        """Serialise one booking an admin asked for by id (SVC-170).

        The counterpart to ``for_viewer(booking, viewer_id=None)``, and the reason
        that one withholds the address. The objection was never that an admin may
        not know where a job is — it is that the bulk listing hands back a page of
        consumers' home addresses whether or not anyone needed one. A request for
        a single booking by id is the targeted, auditable form of the same
        question, so this answers it.

        The doorstep PIN is deliberately *not* reinstated, and the asymmetry is
        the point. It is a proof-of-arrival secret whose entire value is that the
        provider cannot obtain it except from the customer at the door (SVC-169);
        an admin holding it could start a job remotely, and no support workflow
        needs it to answer "where is this provider meant to be going".

        Built on top of ``for_viewer`` rather than beside it so the PIN rule has
        one implementation and cannot drift — this method can only ever *widen*
        what that returns, and it widens by exactly one field.

        Requires ``consumer_address`` to be loaded, like every other path that
        serialises an address: call ``BookingService.attach_addresses`` first.
        """
        response = cls.for_viewer(booking, viewer_id=None)
        if booking.consumer_address is not None:
            response.address = BookingAddress.model_validate(booking.consumer_address)
        return response


class BookingOfferResponse(BaseModel):
    """What a broadcast recipient may see about a job they have not yet accepted.

    Deliberately its own schema rather than a reuse of ``BookingResponse`` (SVC-153).
    This is served to *every* nearby provider, none of whom is assigned yet, so it
    carries only what the Incoming Request Card renders — and notably not
    ``doorstep_otp``, which gates the en_route → in_progress transition and must
    reach the provider from the consumer at the door, not from this API.

    ``service_amount`` is returned as a plain number; the app formats the ₹ string.
    Rupee formatting is not done server-side here — see the ticket's money note.

    ``expires_at`` and ``seconds_remaining`` both describe the same instant and are
    both returned on purpose (SVC-166). The card's countdown must use
    ``seconds_remaining``: it is computed server-side, so a device with a wrong
    clock still counts down correctly, and clock skew is unbounded on a 120-second
    window. ``expires_at`` is the absolute form, for anything needing a real
    instant rather than a duration.

    Neither field carries a default — a defaulted field falls out of OpenAPI
    ``required`` and the generated client types it optional, which invites a
    ``?? 120`` fallback and reintroduces the bug this exists to fix.
    """

    booking_id: UUID
    service_name: str
    service_amount: float
    full_address: str
    distance_km: float
    expires_at: datetime
    seconds_remaining: int


class StartBookingRequest(BaseModel):
    otp: str = Field(..., min_length=4, max_length=4)


class CancelBookingRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)


class BookingListResponse(BaseModel):
    bookings: list[BookingResponse]
    total: int
    page: int
    per_page: int
