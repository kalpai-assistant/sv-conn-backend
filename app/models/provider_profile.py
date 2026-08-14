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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
    # SVC-192 (G33): RazorpayX-side identifiers cached from the composite payout
    # response. Reused on subsequent payouts so we don't create a fresh contact
    # + fund account each time (the composite call would otherwise leave one
    # duplicate contact per payout in RazorpayX's contact book — cosmetic debt
    # per the gap analysis, but debt).
    #
    # Nullable + populated lazily: the first payout for a provider still runs
    # the composite call and stores the ids returned in the response. Every
    # subsequent payout uses the fund-account-reference shape.
    #
    # Invalidation: `razorpayx_fund_account_id` is cleared whenever the
    # provider's payout destination mutates (`update_profile` payout branch,
    # `set_active_payout_method`) because the cached fund account no longer
    # points at the current destination. `razorpayx_contact_id` is only
    # cleared on `remove_payout_method`: the contact represents the person,
    # not the account, so a UPI ↔ bank switch keeps it valid. RazorpayX ids
    # are short (~20 chars in practice) but padded here for future-proofing.
    razorpayx_contact_id: Mapped[str | None] = mapped_column(String(64))
    razorpayx_fund_account_id: Mapped[str | None] = mapped_column(String(64))
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
