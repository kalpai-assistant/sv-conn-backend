import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    icon_url: Mapped[str | None] = mapped_column(String(512))
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
