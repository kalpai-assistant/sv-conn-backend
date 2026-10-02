import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.provider_profile import KYCStatus


class Professional(Base):
    """The person who shows up at the door (SVC-339, team model Phase A).

    Belongs to exactly one Service Provider — the business, which is still the
    ``provider_profiles`` table under its old name. The SP receives dispatch and
    is paid; the Pro does the job. Every SP gets a Pro-for-self that shares its
    ``user_id`` (the solo case: one person, two rows), so the migration that adds
    this table creates one for every existing provider.

    SVC-320 (Phase B, PR 1) writes it in two places: provider signup creates the
    Pro-for-self alongside the SP row, and accepting a team invite creates (or
    reactivates) a team member's row. Dispatch, the ``/pros/*`` endpoints and the
    pick-a-Pro step come later; see ``docs/pro-entity/LLD.md``.
    """

    __tablename__ = "professionals"

    __table_args__ = (
        # 1:N — a Pro belongs to one SP. The pair rather than ``user_id`` alone
        # is the design's choice (LLD "Schema"); it is also what makes the
        # backfill's ``ON CONFLICT DO NOTHING`` idempotent.
        UniqueConstraint("user_id", "service_provider_id", name="uq_professionals_user_sp"),
        Index("ix_professionals_sp_id", "service_provider_id"),
        # Partial: dispatch will only ever ask for the online ones.
        Index(
            "ix_professionals_online",
            "is_online",
            postgresql_where=text("is_online = TRUE"),
        ),
        Index("ix_professionals_location", "last_location", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # References ``provider_profiles.user_id`` (unique) rather than its ``id``
    # primary key, because ``user_id`` is the provider identifier used everywhere
    # else — ``bookings.provider_id`` among them. CASCADE: an SP's team goes with
    # it. SPs are deactivated rather than deleted in practice, so this is rare.
    service_provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_profiles.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    # The Pro's auth account. Equal to ``service_provider_id`` for a Pro-for-self.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_photo_url: Mapped[str | None] = mapped_column(String(512))

    # Aadhaar identity KYC for the person. Payout KYC stays on the SP row. Same
    # ``kycstatus`` enum type as ``provider_profiles.kyc_status``; no new values.
    kyc_status: Mapped[KYCStatus] = mapped_column(
        Enum(KYCStatus),
        nullable=False,
        default=KYCStatus.PENDING,
        server_default=KYCStatus.PENDING.name,
    )
    kyc_docs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    kyc_ai_verdict: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    kyc_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kyc_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kyc_reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    kyc_rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Availability is a per-person signal, so it lives here (LLD "Availability
    # model"). The SP row keeps its own ``is_online`` until dispatch moves over.
    is_online: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_location = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True
    )
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # SVC-320: removing a Pro from a team is a soft delete. The row stays,
    # because bookings and reviews point at it. Only active rows count as team
    # members, and for the one-SP-at-a-time rule at accept-invite.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_self(self) -> bool:
        """True for an SP's Pro-for-self: the same person on both rows."""
        return self.user_id == self.service_provider_id

    # Relationships
    # Lazy by default, like ``ProviderProfile.parent``: under AsyncSession,
    # reading these without eager-loading raises ``MissingGreenlet``, so async
    # callers ``selectinload`` them or read the id column instead. Sync callers
    # (the Celery worker's ``SyncSessionLocal``) can lazy-load as usual.
    service_provider: Mapped["ProviderProfile"] = relationship(
        "ProviderProfile", back_populates="professionals"
    )
    # ``foreign_keys`` because ``kyc_reviewed_by`` is a second FK to ``users``.
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
