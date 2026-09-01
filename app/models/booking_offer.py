"""One provider's invitation to take one booking (SVC-254).

Before this table the dispatch model was push-only: the Celery broadcast found
eligible providers, fired an FCM push at each, and kept no record. If a push
failed to deliver — stale token, denied permission, iOS Safari, incognito —
the provider was deaf to the job and nothing anywhere knew they had been
asked. Persisting the offer gives the provider a second way to find it
(``GET /providers/me/offers``), gives them a way to say no, and makes
"who was offered what, and what did they do" answerable.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OfferResponse(str, enum.Enum):
    """How a provider answered. ``NULL`` on the row means still open.

    Deliberately only the two answers a provider can give. There is no
    "superseded" state for the losers when someone else accepts: the read
    query joins to the booking and filters on it still being REQUESTED and
    unassigned, so a claimed booking drops out of everyone else's list without
    a bulk update on the accept path — which is the hot path where money moves.
    An unanswered row against a closed booking is still distinguishable later
    ("offered, never responded"), which is the analytics case that matters.
    """

    ACCEPTED = "accepted"
    DECLINED = "declined"


class BookingOffer(Base):
    """A booking offered to a provider, and their answer if they gave one."""

    __tablename__ = "booking_offers"

    __table_args__ = (
        # The dispatch task is a Celery task and Celery retries. Without this a
        # retry writes a second row and the provider is offered the same job
        # twice; with it the retry conflicts and can be swallowed.
        UniqueConstraint("booking_id", "provider_id", name="uix_booking_offer"),
        # The poll's hot path: "my rows that are still open".
        Index("ix_booking_offers_provider_response", "provider_id", "response"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False, index=True
    )
    # To ``users.id``, matching ``bookings.provider_id`` rather than the
    # ``provider_profiles.user_id`` that ``provider_services`` uses. Same UUID
    # either way; this is the one the offer endpoints hold, as ``current_user.id``.
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    offered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Null while the offer is open. See OfferResponse for why there are only
    # two values.
    response: Mapped[OfferResponse | None] = mapped_column(Enum(OfferResponse), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # No ``expires_at``. Expiry is ``booking.created_at +
    # BOOKING_REQUEST_TIMEOUT_SECONDS``, computed in ``_offer_expiry`` and swept
    # by ``expire_stale_bookings``, both of which read the setting live. A column
    # here would be a second deadline that disagrees the moment anyone retunes it.

    booking: Mapped["Booking"] = relationship("Booking")  # noqa: F821
