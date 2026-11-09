"""Active mapping of ``(virtual_number) → (customer, SP)`` for a booking (SVC-448).

When a booking confirms, the allocator (SVC-449) picks a free virtual number
from ``virtual_number_pool``, writes one row here, and both parties see that
number instead of each other's real phone. On an incoming Exotel call the
webhook (SVC-450) does a single-row lookup by ``virtual_number_e164`` + caller
to decide who to bridge the call to.

Design decisions (see ticket SVC-448):

* ``virtual_number_e164`` is **denormalized** off ``virtual_number_pool.e164``.
  Webhook lookup is the hot path (one per inbound call) and must be a single
  indexed row read with no join — the partial unique below also lives on this
  column, so the two parties can't double-book a number.
* Phones are E.164 varchar(16), one format everywhere.
* ``released_at`` is NULL for an active mapping. The 48 h grace after booking
  complete/cancel is enforced by ``expires_at``; the release cron walks
  ``expires_at < now() AND released_at IS NULL``.
* ``call_mappings.customer_phone_e164`` and ``sp_phone_e164`` are snapshots of
  the real numbers at allocation time: if either party later changes their
  phone the live mapping stays valid, and if the user row is deleted
  (SVC-367 tombstone) we still know who to route to for the remaining 48 h.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CallMapping(Base):
    """One active allocation of a virtual number to a (booking, customer, SP) tuple.

    The row is created by ``CallMaskingService.allocate_mapping`` and closed by
    ``release_mapping`` (sets ``released_at``). The partial unique index on
    ``virtual_number_e164 WHERE released_at IS NULL`` is what DB-level prevents
    an active number from being double-booked: a concurrent allocator can race
    into the ``INSERT`` but only one succeeds, and the loser retries with a
    different number.
    """

    __tablename__ = "call_mappings"

    __table_args__ = (
        # One active mapping per number at a time. Partial so historical rows
        # (released_at IS NOT NULL) don't trip the constraint.
        Index(
            "uq_call_mappings_virtual_number_active",
            "virtual_number_e164",
            unique=True,
            postgresql_where="released_at IS NULL",
        ),
        # Lookup by booking: the customer/SP app asks "what's the virtual
        # number for this booking?" via the call-mapping API.
        Index("ix_call_mappings_booking_id", "booking_id"),
        # Release cron scan: find active mappings whose grace has run out.
        Index(
            "ix_call_mappings_expires_at_active",
            "expires_at",
            postgresql_where="released_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    virtual_number_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("virtual_number_pool.id"), nullable=False
    )
    # Denormalized from ``virtual_number_pool.e164`` — the webhook lookup hits
    # this directly without a join. The partial unique above lives here too.
    virtual_number_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False
    )
    # Snapshot of the customer's real phone at allocation time. Keeps the
    # mapping routable for the full 48 h grace even if the user updates or
    # tombstones (SVC-367) their row mid-mapping.
    customer_phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    sp_phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # booking end + 48 h grace, set by the allocator. The release cron
    # (SVC-449) picks up mappings whose expiry passed with released_at still NULL.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL for an active mapping. Set by ``release_mapping``.
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
