import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.price_unit import PRICE_UNIT_CHECK


class SpServicePricing(Base):
    """What one Service Provider charges for one service (SVC-408, epic SVC-406).

    One row per (SP, service). ``sp_id`` is ``provider_profiles.user_id``, the SP
    identifier used everywhere else (``bookings.provider_id``,
    ``professionals.service_provider_id``), not the profile's own ``id``.

    ``price_unit`` is copied from the service rather than joined, so the ranking
    query on the hot path doesn't need a join. It is a ``PriceUnit`` value,
    varchar + CHECK like ``services.price_unit``. Both FKs are RESTRICT: SPs and
    services are soft-deleted, so a hard delete that would orphan a price has to
    clean the price up first.

    Nothing reads or writes this table yet: the SP pricing surface is SVC-412 and
    the ranking SVC-409.
    """

    __tablename__ = "sp_service_pricing"

    __table_args__ = (
        UniqueConstraint("sp_id", "service_id", name="uq_sp_service_pricing_sp_service"),
        # The "cheapest in area" lookup starts from a service.
        Index("ix_sp_service_pricing_service_active", "service_id", "is_active"),
        CheckConstraint(PRICE_UNIT_CHECK, name="ck_sp_service_pricing_price_unit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sp_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "provider_profiles.user_id",
            name="fk_sp_service_pricing_sp_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("services.id", name="fk_sp_service_pricing_service_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Numeric(12, 2) like every other money column (SVC-150).
    price_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    price_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
