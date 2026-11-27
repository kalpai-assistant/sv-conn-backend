"""An SP's proposal for a new catalog service, and how an admin settled it (SVC-436).

Lifecycle: ``PENDING`` → one of ``APPROVED`` (``created_service_id`` points at
the new catalog entry), ``REJECTED_WITH_REASON`` (``rejection_reason`` says why)
or ``REJECTED_WITH_RECOMMENDATION`` (``recommended_service_id`` is the existing
service the SP should price instead). ``resolved_by`` / ``resolved_at`` are set
on every resolution.

The enum-like columns are varchar + CHECK, not Postgres ENUMs, so adding a value
is a one-line constraint swap. ``proposed_price_unit`` takes the ``PriceUnit``
values; ``PRICE_UNIT_CHECK`` names the ``price_unit`` column, so the CHECK here
is built from the same enum under this column's name.

``proposed_price`` is the SP's suggestion, in rupees, that the admin anchors the
approved price on. Nothing books against it.
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.price_unit import PriceUnit


class ServiceRequestStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED_WITH_REASON = "REJECTED_WITH_REASON"
    REJECTED_WITH_RECOMMENDATION = "REJECTED_WITH_RECOMMENDATION"


REJECTED_STATUSES: tuple[ServiceRequestStatus, ...] = (
    ServiceRequestStatus.REJECTED_WITH_REASON,
    ServiceRequestStatus.REJECTED_WITH_RECOMMENDATION,
)


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


# The CHECK bodies, shared by the model and the migration's test.
SERVICE_REQUEST_STATUS_CHECK = _in("status", ServiceRequestStatus)
PROPOSED_PRICE_UNIT_CHECK = _in("proposed_price_unit", PriceUnit)


class ServiceRequest(Base):
    __tablename__ = "service_requests"
    __table_args__ = (
        CheckConstraint(SERVICE_REQUEST_STATUS_CHECK, name="ck_service_requests_status"),
        CheckConstraint(PROPOSED_PRICE_UNIT_CHECK, name="ck_service_requests_proposed_price_unit"),
        # The quota check's hot path: an SP's PENDING count.
        Index("ix_service_requests_sp_id_status", "sp_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RESTRICT (no ondelete): accounts are tombstoned, never deleted (SVC-367).
    sp_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_profiles.user_id"), nullable=False
    )
    # 200 = services.name, since an approved request becomes a service.
    proposed_name: Mapped[str] = mapped_column(String(200), nullable=False)
    proposed_description: Mapped[str] = mapped_column(Text, nullable=False)
    # RESTRICT: categories and services are only ever soft-deleted (is_active).
    proposed_category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False
    )
    proposed_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    proposed_price_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ServiceRequestStatus.PENDING.value,
        server_default=ServiceRequestStatus.PENDING.value,
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    recommended_service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=True
    )
    # SET NULL (Vipul, SVC-436 comment 14840): keep the record, drop attribution.
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# The admin review queue: newest first within a status. Declared after the class
# because DESC needs the column object.
Index(
    "ix_service_requests_status_created_at",
    ServiceRequest.status,
    ServiceRequest.created_at.desc(),
)
