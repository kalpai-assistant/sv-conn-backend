import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserRole(str, enum.Enum):
    CONSUMER = "consumer"
    PROVIDER = "provider"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(15), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    fcm_token: Mapped[str | None] = mapped_column(String(255))
    # SVC-371: which app registered ``fcm_token`` (``android`` | ``ios`` | ``web``),
    # so the release push reaches only the platform being released. NULL = not
    # reported (a client older than SVC-371); such tokens still get every release
    # push. Cleared whenever ``fcm_token`` is. Plain string, not a PG enum.
    fcm_platform: Mapped[str | None] = mapped_column(String(10))
    # SVC-240: stamped on every successful verify-otp. NULL for users who
    # signed up before the column existed and haven't signed in since.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SVC-367: set when the user deleted their account. The row is a tombstone,
    # not deleted, because ~20 FKs (bookings, reviews, ledger, payouts) point at
    # it; ``phone`` then holds a non-phone placeholder, so the number is free to
    # sign up again as a new account. See ``AccountService.delete_account``.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    # ``foreign_keys`` disambiguates: SVC-307 added a second FK from
    # ``provider_profiles`` to ``users`` (``kyc_reviewed_by``), and without
    # this SQLAlchemy can't decide which FK this relationship should join on.
    provider_profile: Mapped["ProviderProfile"] = relationship(
        "ProviderProfile",
        back_populates="user",
        uselist=False,
        foreign_keys="ProviderProfile.user_id",
    )
    addresses: Mapped[list["Address"]] = relationship("Address", back_populates="user")
    bookings_as_consumer: Mapped[list["Booking"]] = relationship(
        "Booking", back_populates="consumer", foreign_keys="Booking.consumer_id"
    )
    bookings_as_provider: Mapped[list["Booking"]] = relationship(
        "Booking", back_populates="provider", foreign_keys="Booking.provider_id"
    )
