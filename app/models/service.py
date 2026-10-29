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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Service(Base):
    """An individual service offered by a provider under a category."""

    __tablename__ = "services"

    __table_args__ = (
        # Partial index: fast catalog browsing by category for active services only
        Index("idx_services_category_active", "category_id", postgresql_where="is_active = true"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # The root money value — every booking's split derives from it, so it must be
    # exact before anything downstream can be (SVC-150).
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # SVC-293: full URLs to the service's banner media in S3. Both nullable —
    # the frontend falls through to sub-category / L1 / colour block when
    # they are, so a service without curated media renders intentionally.
    banner_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    banner_video_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # SVC-340: what the Pro does (ordered) and what the service doesn't cover,
    # shown on the service detail page. NULL means "no data" and the frontend
    # hides the section. Edited by SQL only — there is no admin UI.
    # SVC-400: ``steps`` may also hold the richer SVC-392 shape,
    # ``[{title, duration_minutes?, icon?}]``; the frontend accepts both.
    steps: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    exclusions: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # SVC-400: catalogue content for the SVC-392 detail page. All NULL until the
    # content lands; the frontend hides each section (or falls back) on NULL.
    hero_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    included_items: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # [{question, answer}]
    faqs: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    # [{before_url, after_url}]; stays empty until SVC-401.
    before_after_photos: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    category: Mapped["Category"] = relationship("Category", back_populates="services")
    # `lazy="raise_on_sql"`: routes that render variants (GET /services,
    # service detail, cart) call an attach helper explicitly. A caller who
    # forgets should fail loudly here rather than trigger a MissingGreenlet
    # inside pydantic, or on a list route, one implicit lookup per row.
    variants: Mapped[list["ServiceVariant"]] = relationship(
        "ServiceVariant",
        back_populates="service",
        lazy="raise_on_sql",
        order_by="ServiceVariant.sort_order",
    )
