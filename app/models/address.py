import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from geoalchemy2 import Geography
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.geo import point_lat_lng

if TYPE_CHECKING:
    from app.models.user import User


class Address(Base):
    """Consumer saved addresses with optional PostGIS location point."""

    __tablename__ = "addresses"

    __table_args__ = (
        Index("idx_addresses_location", "location", postgresql_using="gist"),
        # SVC-277: the address book reads live rows only, so the index covers
        # just those. Declared here as well as in the migration — Alembic
        # compares the models against the database, and an index present in one
        # but not the other reads as a pending drop.
        Index(
            "ix_addresses_user_live",
            "user_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    # "home" | "work" | "other"
    label: Mapped[str] = mapped_column(String(50), nullable=False, default="home")
    full_address: Mapped[str] = mapped_column(String(500), nullable=False)
    # PostGIS POINT for distance calculations
    location = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # SVC-277: soft delete. `bookings.consumer_address_id` is an unqualified FK,
    # so removing a row Postgres knows a booking points at raises — and almost
    # every address has a booking. Hiding instead of deleting keeps the FK
    # satisfied and keeps a booking's address resolvable for history.
    #
    # The filtering rule this creates is asymmetric and worth stating once:
    # the *address book* hides deleted rows; anything resolving a *booking's*
    # address must not, or the geo-fence stops matching providers and the
    # booking dies as "no partners found nearby" with nothing logged.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="addresses")

    # Readable coordinates, for callers that hand the address to a maps app
    # rather than measure with it (SVC-170). Plain Python properties decoding the
    # `location` value already in memory: no SQL, so no lazy load to trip over on
    # an async session, and they work the same on a row loaded from the database
    # and on one created in this session. `None` whenever the address has no
    # location — `CreateAddressRequest` accepts an address without coordinates.
    #
    # Not `column_property(ST_Y(...))`: an INSERT does not populate a computed
    # column, so reading it on an address created earlier in the same session
    # would emit a query — a MissingGreenlet under AsyncSession, and exactly what
    # the test suite does. See `point_lat_lng` for the full reasoning.
    @property
    def lat(self) -> float | None:
        return point_lat_lng(self.location)[0]

    @property
    def lng(self) -> float | None:
        return point_lat_lng(self.location)[1]
