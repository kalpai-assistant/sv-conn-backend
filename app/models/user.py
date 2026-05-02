import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Boolean, Enum, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class UserRole(str, enum.Enum):
    CONSUMER = "consumer"
    PROVIDER = "provider"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    phone: Mapped[str] = mapped_column(
        String(15), unique=True, nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    fcm_token: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    provider_profile: Mapped["ProviderProfile"] = relationship(
        "ProviderProfile", back_populates="user", uselist=False
    )
    addresses: Mapped[list["Address"]] = relationship(
        "Address", back_populates="user"
    )
    bookings_as_consumer: Mapped[list["Booking"]] = relationship(
        "Booking", back_populates="consumer", foreign_keys="Booking.consumer_id"
    )
    bookings_as_provider: Mapped[list["Booking"]] = relationship(
        "Booking", back_populates="provider", foreign_keys="Booking.provider_id"
    )
