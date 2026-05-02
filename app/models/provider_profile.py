import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Boolean, Integer, Float, ForeignKey, Enum, Index
from sqlalchemy.dialects.postgresql import UUID
from geoalchemy2 import Geography
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class KYCStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ProviderProfile(Base):
    __tablename__ = "provider_profiles"

    __table_args__ = (
        # GiST spatial index — enables fast ST_DWithin queries
        Index("idx_provider_service_area", "service_area_center", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    # PII stored AES-256 encrypted; encryption handled in service layer
    aadhaar_encrypted: Mapped[str | None] = mapped_column(String(512))
    pan_encrypted: Mapped[str | None] = mapped_column(String(512))
    upi_id_encrypted: Mapped[str | None] = mapped_column(String(512))
    bank_account_encrypted: Mapped[str | None] = mapped_column(String(1024))
    profile_photo_url: Mapped[str | None] = mapped_column(String(512))

    # PostGIS geography column for location-based queries
    service_area_center = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True
    )
    service_area_radius_km: Mapped[int] = mapped_column(Integer, default=10)

    avg_rating: Mapped[float] = mapped_column(Float, default=0.0)
    total_completed_jobs: Mapped[int] = mapped_column(Integer, default=0)
    razorpay_linked_account_id: Mapped[str | None] = mapped_column(String(255))
    kyc_status: Mapped[KYCStatus] = mapped_column(
        Enum(KYCStatus), default=KYCStatus.PENDING
    )
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="provider_profile")
