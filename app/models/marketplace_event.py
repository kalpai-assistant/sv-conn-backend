import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Sent by the customer app through POST /analytics/events (SVC-419).
CLIENT_EVENTS = (
    "marketplace.sheet_opened",
    "marketplace.sheet_abandoned",
    "marketplace.sp_selected",
    "marketplace.price_change_at_checkout",
    "marketplace.tile_coming_soon_tapped",
)
# Written by our own code, in the transaction of the change they describe. The
# ingest endpoint refuses these names.
SERVER_EVENTS = ("sp.price_set", "booking.marketplace_created")


class MarketplaceEvent(Base):
    """One marketplace analytics event (SVC-419). ADMIN.md lists every event.

    A typed id goes in its column, never in ``payload``: ``user_id`` (who did
    it: the customer, or the SP for ``sp.price_set``), ``sp_id``, ``booking_id``,
    ``service_id``, ``service_variant_id``. Each is a plain FK, as on the other
    log tables; users are tombstoned and bookings/services never hard-deleted,
    so no FK blocks a delete. Everything else is in ``payload``, with money as
    a 2-decimal string ("2400.00").

    ``event_id`` dedupes: the app sends its own UUID and a retry is ignored.
    ``occurred_at`` is the phone's clock (NULL for server events); reports use
    ``created_at``.

    Written only by ``app/services/marketplace_events.py``. Account deletion
    sets ``user_id`` to NULL; nothing else updates or deletes a row.
    """

    __tablename__ = "marketplace_events"

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_marketplace_events_event_id"),
        Index("ix_marketplace_events_name_created_at", "name", "created_at"),
        Index("ix_marketplace_events_service_id", "service_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    sp_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=True
    )
    service_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_variants.id"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
