import enum
import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.geo import point_lat_lng


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
        # SVC-319: "find the team members of X". Partial, so it stays empty while
        # every provider is solo — which is every provider on beta today.
        Index(
            "ix_provider_profiles_parent",
            "parent_provider_id",
            postgresql_where="parent_provider_id IS NOT NULL",
        ),
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
    # SVC-336: the business the provider trades as, and its logo — separate from
    # the person (``users.name``, ``profile_photo_url``). Onboarding requires the
    # name; the image is optional and the app shows a default logo when it is
    # NULL. Both nullable because providers created before onboarding asked for
    # them have neither.
    business_name: Mapped[str | None] = mapped_column(String(100))
    business_image_url: Mapped[str | None] = mapped_column(String(512))
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
    # SVC-307: full KYCVerdict payload from the AI verifier, stored for audit
    # + surfaced on the admin review page. Nullable — pre-SVC-307 profiles
    # were never scored and a placeholder would misrepresent that on the UI.
    kyc_ai_verdict: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # SVC-307: who moved this out of SUBMITTED. Populated by the auto-approve
    # path with the verifier-bot's id; manual admin approvals may stamp it in
    # a follow-up ticket. FK to users(id) with ON DELETE SET NULL so removing
    # a deprecated bot row doesn't cascade-null profiles it has never touched.
    kyc_reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    is_online: Mapped[bool] = mapped_column(Boolean, default=False)

    # SVC-319 (team model, Phase A): the team owner this provider works under.
    # NULL = a solo provider, or a team owner — the two look the same until a
    # member points at the owner. Set = a team member. Chain depth is 1, enforced
    # in service code when invites are accepted (Phase B), not by the schema.
    # References ``user_id`` (unique) rather than the ``id`` primary key, because
    # ``user_id`` is the provider identifier used everywhere else, e.g.
    # ``bookings.provider_id``. SET NULL: an owner row removed turns its members
    # solo rather than deleting them.
    parent_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "provider_profiles.user_id",
            name="fk_provider_profiles_parent",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    # Relationships
    # ``foreign_keys=[user_id]`` because SVC-307 added a second FK to
    # ``users`` (``kyc_reviewed_by``) — without this, SQLAlchemy can't
    # decide which FK the ``user`` relationship should join on and refuses
    # to initialise the mapper.
    user: Mapped["User"] = relationship(
        "User", back_populates="provider_profile", foreign_keys=[user_id]
    )
    # SVC-319: the owner's own profile row. Self-referencing, so ``remote_side``
    # names the column on the *parent* side of the join. Lazy by default: under
    # AsyncSession, reading ``.parent`` without eager-loading it raises
    # ``MissingGreenlet``, so callers load it explicitly (``selectinload``) or
    # read ``parent_provider_id`` instead — as ``payout_target_id`` does.
    parent: Mapped["ProviderProfile | None"] = relationship(
        "ProviderProfile",
        remote_side=[user_id],
        foreign_keys=[parent_provider_id],
    )
    # SVC-339: the people who do this SP's jobs — one row (the Pro-for-self) for
    # every provider today. Lazy by default; under AsyncSession load it with
    # ``selectinload``. ``passive_deletes`` leaves removing them to the FK's
    # ON DELETE CASCADE instead of having the ORM null out a NOT NULL column.
    professionals: Mapped[list["Professional"]] = relationship(
        "Professional",
        back_populates="service_provider",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def service_area_lat(self) -> float | None:
        """Latitude of the service-area centre, or None when it was never set.

        SVC-279: the centre goes *in* through ``PATCH /providers/me`` but never
        came back out, so a provider could overwrite their service area but
        never see it. A Location screen cannot centre a map, pre-fill a pin, or
        show that anything was saved without this.

        Decoded in Python via ``point_lat_lng`` rather than a SQL
        ``ST_X``/``ST_Y`` column_property: the latter is not populated by the
        INSERT that creates the row, so reading it in the same session emits a
        query — a ``MissingGreenlet`` under AsyncSession, which is how the test
        suite builds profiles. The helper also absorbs the WKBElement-vs-EWKT
        duality that broke address editing in #151.
        """
        return point_lat_lng(self.service_area_center)[0]

    @property
    def service_area_lng(self) -> float | None:
        """Longitude of the service-area centre. See :attr:`service_area_lat`."""
        return point_lat_lng(self.service_area_center)[1]

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

    # Deprecated — use booking.professional.service_provider_id going forward
    # (SVC-339). Built for SVC-319's single-entity design, which the two-entity
    # model superseded; kept until a cleanup ticket drops it with
    # ``parent_provider_id``.
    @property
    def payout_target_id(self) -> uuid.UUID:
        """The provider whose payout account receives this provider's earnings.

        A team member → their team owner; a solo provider or team owner → themselves
        (SVC-319). Kept on the model so the "parent, else self" rule lives in one
        place rather than being restated at each call site.

        Not yet read by any payout code: payouts wait on RazorpayX, so routing
        them through this is deferred (Vipul, 2026-09-29). Reads the column, not
        the ``parent`` relationship, so it never triggers a lazy load.
        """
        return self.parent_provider_id or self.user_id
