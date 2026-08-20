"""One priced option of a service.

A service ("Full home deep cleaning") carries multiple variants ("1 BHK" /
"2 BHK" / "3 BHK" / "4 BHK"), each with its own price + provider payout.
Introduced by SVC-pricing (2026-08-20 pricing sheet load) — the old
one-price-per-service shape flattened variants into the service name and
lost the grouping.

Vehicle-type variants (Hatchback / Sedan / SUV / MUV for a car-wash
service) use the same table — one variant per vehicle type, subcategory
carries the sheet's inner grouping ("Wash" / "Interior" / "Exterior").

Money on this row is authoritative for booking pricing. When a booking
is created, ``base_price`` / ``provider_payout`` / ``platform_commission``
are snapshotted into the corresponding ``booking_items`` columns — a
later price update on the variant must not retroactively change the
amount owed on an already-placed booking.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ServiceVariant(Base):
    __tablename__ = "service_variants"

    __table_args__ = (
        UniqueConstraint("service_id", "label", name="uix_variant_service_label"),
        Index(
            "idx_variants_service_active",
            "service_id",
            postgresql_where="is_active = true",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # Vertical sheets group variants under an inner category (Basic /
    # Installation / Filter / Wash / Interior / ...). Nullable because
    # the "Pricing" summary sheet doesn't carry this concept.
    subcategory: Mapped[str | None] = mapped_column(String(100))
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    provider_payout: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    platform_commission: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Percent, not amount — the amount is derived from base_price at display
    # time. 0 = "no GST modelled yet" (the launch summary rows).
    gst_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default="0")
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    materials_note: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    service: Mapped["Service"] = relationship("Service", back_populates="variants")
