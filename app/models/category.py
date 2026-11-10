import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Category(Base):
    """Service categories.

    Self-referential parent/child hierarchy (SVC-286). Level-1 categories are
    the seven illustrated tiles the consumer home shows; level-2 categories
    are the historical flat catalogue promoted to sub-categories under those
    tiles. Every ``Service`` still points at an L2 (or an L1 directly, in the
    Miscellaneous case) via ``services.category_id`` — the hierarchy is
    additive, so existing bookings, provider subscriptions and service ids
    remain unchanged.
    """

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(
            "is_sensitive = false OR bounds_pct IS NOT NULL",
            name="ck_categories_sensitive_needs_bounds",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    icon_url: Mapped[str | None] = mapped_column(String(512))
    # SVC-430: a key from app.seed_data.catalog_icons.CATALOG_ICON_KEYS, or NULL.
    # The consumer app still keys its icons on the slug and ignores icon_url.
    icon_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # SVC-286: NULL parent_id ⇒ Level-1 (home tile). Non-NULL ⇒ Level-2
    # (sub-category rendered inside a category page).
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # SVC-428: pricing config, edited at /admin/pricing/categories. Set on every
    # row, L1 and L2 alike; the console groups L2 under L1. Inert until the
    # marketplace stories read it: commission (SVC-410) and the sensitive-category
    # price bounds (SVC-406). Booking commission still comes from
    # service_variants.platform_commission / settings.PLATFORM_COMMISSION_PERCENT.
    # Never on the public GET /categories.
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # Required while is_sensitive (the CHECK above), NULL otherwise.
    bounds_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    platform_commission_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("20.00"), server_default=text("20.00")
    )
    platform_commission_min: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("30.00"), server_default=text("30.00")
    )
    commission_gst_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("18.00"), server_default=text("18.00")
    )

    # Relationships
    services: Mapped[list["Service"]] = relationship("Service", back_populates="category")
    parent: Mapped["Category | None"] = relationship(
        "Category",
        remote_side="Category.id",
        back_populates="children",
    )
    children: Mapped[list["Category"]] = relationship(
        "Category",
        back_populates="parent",
        order_by="Category.sort_order",
    )
