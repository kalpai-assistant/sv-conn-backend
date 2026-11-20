import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

KIND_COMING_SOON = "coming_soon"
KIND_NO_PROS_ONLINE = "no_pros_online"
KINDS = (KIND_COMING_SOON, KIND_NO_PROS_ONLINE)

# Open = not yet notified and not cancelled. Expiry is not part of it: an
# expired alert writes nothing, and every reader filters ``expires_at > now``.
OPEN_PREDICATE = "notified_at IS NULL AND cancelled_at IS NULL"


class ServiceAlert(Base):
    """A customer's "Notify me" on a service tile (SVC-484).

    It covers "Coming soon" (``coming_soon``: no SP serves the point) and
    "No pros online right now" (``no_pros_online``: served, but nobody
    listable). The server picks the kind at the moment of the tap. The
    ``notify_service_alerts`` sweep pushes once, then stamps ``notified_at``.

    One open alert per (user, service), held by a partial unique index. A new
    tap from another point moves the open row and restarts ``expires_at``
    (created + 45 days). An expired alert is never pushed, and nothing is
    written when it expires. ``location`` is the point the tile was shown for,
    so the sweep can pass it straight into the dispatch stage rules.

    Account deletion deletes the user's rows, because they hold a location.
    """

    __tablename__ = "service_alerts"

    __table_args__ = (
        CheckConstraint("kind IN ('coming_soon', 'no_pros_online')", name="ck_service_alerts_kind"),
        Index(
            "uq_service_alerts_open_user_service",
            "user_id",
            "service_id",
            unique=True,
            postgresql_where=text(OPEN_PREDICATE),
        ),
        Index(
            "ix_service_alerts_open_expires_at",
            "expires_at",
            postgresql_where=text(OPEN_PREDICATE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    location = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
