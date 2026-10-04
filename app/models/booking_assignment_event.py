import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The only kinds there are (SVC-333). The DB CHECK constraint is built from this
# tuple, so adding a kind means a migration as well as a new entry here.
ASSIGNMENT_EVENT_KINDS = ("claimed", "repicked", "declined", "released", "provider_cancelled")


class BookingAssignmentEvent(Base):
    """Who was sent to a booking, who changed that, and when (SVC-333).

    One row per change of the booking's Pro: a claim (accept or assign), a
    re-pick, a Pro decline, an SP release, or a provider cancel that reopens
    the booking. ``from_professional_id`` / ``to_professional_id`` are the Pro
    before and after (NULL = nobody); ``actor_user_id`` is who did it (NULL =
    the system).

    **Append-only.** Rows are written only by ``BookingService.record_assignment``,
    in the same transaction as the change they describe, and no code path
    updates or deletes them. Don't add helpers that do.

    Kept apart from ``booking_status_logs`` on purpose: that table feeds the
    consumer's ``status_history``, where a re-pick would show up as a second
    "Accepted". This one is read only by the admin timeline, so there is
    deliberately no relationship on ``Booking`` for a consumer projection to
    pick up.
    """

    __tablename__ = "booking_assignment_events"

    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in ASSIGNMENT_EVENT_KINDS) + ")",
            name="ck_booking_assignment_events_kind",
        ),
        Index("ix_booking_assignment_events_booking_id_created_at", "booking_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    from_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True
    )
    to_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True
    )
    # NULL = the system did it, not a person.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
