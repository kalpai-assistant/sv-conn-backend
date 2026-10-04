import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BookingStatusLog(Base):
    """Immutable audit trail of every booking state transition."""

    __tablename__ = "booking_status_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), index=True
    )
    from_status: Mapped[str] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    # NULL = the system did it, not a person (SVC-344: the auto-cancel of a
    # paid booking nobody took). Never exposed by the API.
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # Column named "metadata" in DB; using metadata_ to avoid conflicts
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    # Free-text reason for the transition. Populated for admin-driven state
    # changes (SVC-299 force-cancel: the reason the operator typed in the
    # confirm dialog); nullable everywhere else because most transitions have
    # no operator narrative to record.
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    booking: Mapped["Booking"] = relationship("Booking", back_populates="status_logs")
