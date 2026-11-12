import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The DB CHECK is built from this tuple, so a new kind needs a migration too.
SUPPORT_NOTE_KINDS = ("note", "contacted")
SUPPORT_NOTE_MAX_LENGTH = 2000


class BookingSupportNote(Base):
    """A support agent's note on a booking, or a "contacted the customer" mark
    (SVC-433). Every admin role can read and add them.

    ``kind`` tells the two apart, so "did we call them back?" is a query rather
    than a read through free text. A ``contacted`` entry may have no text. A
    ``note`` must have some.

    **Append-only.** Nothing updates or deletes these rows, and no route does:
    support context should accumulate, so nobody can later say "I never said
    that". ``author_user_id`` is the admin who wrote it.
    """

    __tablename__ = "booking_support_notes"

    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in SUPPORT_NOTE_KINDS) + ")",
            name="ck_booking_support_notes_kind",
        ),
        CheckConstraint(
            "kind = 'contacted' OR (body IS NOT NULL AND btrim(body) <> '')",
            name="ck_booking_support_notes_note_has_body",
        ),
        CheckConstraint(
            f"body IS NULL OR char_length(body) <= {SUPPORT_NOTE_MAX_LENGTH}",
            name="ck_booking_support_notes_body_length",
        ),
        Index("ix_booking_support_notes_booking_id_created_at", "booking_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    # Stamped by the app as well as the DB: Postgres now() is the transaction's
    # start, so two entries in one transaction would tie and list in no order.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
