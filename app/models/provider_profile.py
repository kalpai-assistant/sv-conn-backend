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


class PayoutMethod(str, enum.Enum):
    """Which stored destination a provider's payouts actually go to.

    A provider may keep both a UPI handle and a bank account on file (SVC-148:
    switching does not erase the old one). This is the single source of truth for
    which one the money is sent to — the payout must never infer it from which
    columns happen to be populated.
    """

    UPI = "upi"
    BANK = "bank"


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
    # Payout destination (separate from KYC UPI/bank above). Used to pay providers
    # via RazorpayX; AES-256 encrypted like other PII.
    payout_vpa_encrypted: Mapped[str | None] = mapped_column(String(512))
    payout_account_number_encrypted: Mapped[str | None] = mapped_column(String(1024))
    payout_ifsc_encrypted: Mapped[str | None] = mapped_column(String(512))
    # Which of the above the payout actually uses. Both may be populated at once —
    # saving a method never erases the other — so this, not column presence, decides
    # where the money goes (SVC-148).
    payout_method: Mapped[PayoutMethod | None] = mapped_column(Enum(PayoutMethod))
    # Ready-to-show label for the saved method: "••••8901" for a bank account, or the
    # VPA in full. Stored in the clear on purpose — it holds only what we have agreed
    # is safe to display, which keeps decryption off the profile read path entirely.
    payout_display: Mapped[str | None] = mapped_column(String(120))
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

    @property
    def payout_set(self) -> bool:
        """True when a payout destination is active.

        Derived so the API can expose 'already added / skip' state to the frontend
        without ever returning the encrypted PII itself.

        Keyed on ``payout_method`` rather than on which encrypted columns are
        populated (SVC-148): a provider who switched methods still has the old one
        stored, so column presence no longer answers "can we pay this provider".
        """
        return self.payout_method is not None

    @property
    def payout_methods_stored(self) -> list["PayoutMethod"]:
        """Which methods have details on file, active or not (SVC-152).

        The companion to :attr:`payout_set`, which answers "where does the money go"
        but says nothing about the *other* method. Without this the frontend cannot
        tell whether "switch back to your bank account" is even possible, so it fires
        the switch and reads the 400 as its answer — a button that may be a dead end.

        Derived from column presence, so the profile read path stays decryption-free:
        it names which slots are filled and never what is in them. Same disclosure
        category as ``payout_set``, which is already public on this response.

        A bank account needs *both* the account number and the IFSC — either alone
        cannot be paid to, and reporting it as stored would put back exactly the dead
        -end switch this removes.
        """
        stored = []
        if self.payout_vpa_encrypted:
            stored.append(PayoutMethod.UPI)
        if self.payout_account_number_encrypted and self.payout_ifsc_encrypted:
            stored.append(PayoutMethod.BANK)
        return stored
