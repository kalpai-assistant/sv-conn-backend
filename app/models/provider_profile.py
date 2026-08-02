import enum
import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class KYCStatus(str, enum.Enum):
    PENDING = "pending"  # No docs uploaded yet
    SUBMITTED = "submitted"  # Docs uploaded, awaiting admin review
    VERIFIED = "verified"  # Admin approved — provider can subscribe + earn
    REJECTED = "rejected"  # Admin rejected — see kyc_rejection_reason


class ProviderProfile(Base):
    __tablename__ = "provider_profiles"

    __table_args__ = (
        # GiST spatial index — enables fast ST_DWithin queries
        Index("idx_provider_service_area", "service_area_center", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    # PII stored AES-256 encrypted; encryption handled in service layer
    aadhaar_encrypted: Mapped[str | None] = mapped_column(String(512))
    pan_encrypted: Mapped[str | None] = mapped_column(String(512))
    upi_id_encrypted: Mapped[str | None] = mapped_column(String(512))
    bank_account_encrypted: Mapped[str | None] = mapped_column(String(1024))
    profile_photo_url: Mapped[str | None] = mapped_column(String(512))
    # Languages the provider speaks (e.g. ["Hindi", "English"]). text[] so we can
    # filter discovery by language (`'Hindi' = ANY(languages)`); defaults to empty.
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )

    # PostGIS geography column for location-based queries
    service_area_center = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True
    )
    service_area_radius_km: Mapped[int] = mapped_column(Integer, default=10)

    avg_rating: Mapped[float] = mapped_column(Float, default=0.0)
    total_completed_jobs: Mapped[int] = mapped_column(Integer, default=0)
    razorpay_linked_account_id: Mapped[str | None] = mapped_column(String(255))
    kyc_status: Mapped[KYCStatus] = mapped_column(Enum(KYCStatus), default=KYCStatus.PENDING)
    # KYC document storage. JSONB lets us add fields (e.g. video selfie, address proof)
    # without a new migration. Only S3 keys are stored — never raw bytes, never public
    # URLs. Read access goes through a presigned URL endpoint admin-only.
    kyc_docs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    kyc_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kyc_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kyc_rejection_reason: Mapped[str | None] = mapped_column(String(500))

    is_online: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="provider_profile")
