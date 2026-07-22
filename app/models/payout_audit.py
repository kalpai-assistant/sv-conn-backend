import uuid
from datetime import datetime
from sqlalchemy import String, Integer, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class PayoutAudit(Base):
    """Immutable audit trail of provider payouts.

    One row per payout attempt — recorded on success and on failure — so a
    money-moving action always answers "who paid whom, how much, and what
    happened". ``outcome`` carries the RazorpayX status on success (e.g.
    ``processing``) or ``failed`` when the payout call errored.

    Not every row comes from a human: the reconciliation job also writes here
    (``source == "reconciliation"``, ``admin_user_id`` null).
    """

    __tablename__ = "payout_audits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Null for non-human actions (e.g. the reconciliation job); see ``source``.
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True, nullable=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    # Who initiated this row: "admin" (default) or "reconciliation".
    source: Mapped[str] = mapped_column(
        String(20), default="admin", server_default="admin"
    )
    amount_paise: Mapped[int] = mapped_column(Integer)
    booking_count: Mapped[int] = mapped_column(Integer)
    # RazorpayX payout id — null when the payout call failed before one was created.
    payout_id: Mapped[str | None] = mapped_column(String(255))
    # Batch key echoed to RazorpayX as reference_id; lets reconciliation map an
    # orphaned payout back to its bookings. Null on older rows.
    reference_id: Mapped[str | None] = mapped_column(String(255), index=True)
    outcome: Mapped[str] = mapped_column(String(20))
    # Failure detail (sans secrets); null on success.
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
