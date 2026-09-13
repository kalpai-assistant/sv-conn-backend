"""Curated relationships between services (SVC-291).

Powers the three related-services surfaces on the redesigned service detail
page (SVC-292): a cross-domain "hook" rail, an obvious "upsell" rail, and a
same-context "related" list. Rows are seeded algorithmically from category
proximity (see ``scripts/seed_service_relationships.py``) with SQL-editable
overrides — no admin UI at beta scale.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RelationshipType(str, enum.Enum):
    """The three surfaces a related-service entry can appear under.

    Stored as VARCHAR(20) rather than a Postgres ENUM — the ticket's schema
    specifies VARCHAR and it keeps ``alter type`` migrations off the table
    if we ever add a fourth surface. String subclass so ``"hook" == HOOK``.
    """

    HOOK = "hook"
    UPSELL = "upsell"
    RELATED = "related"


class ServiceRelationship(Base):
    """One directed edge from a source service to a related service.

    Directed on purpose: "Men's Haircut hooks Vehicle Wash" is not the same
    claim as "Vehicle Wash hooks Men's Haircut", and forcing a symmetric
    relationship would inflate the seed's row count without adding signal.
    If a reverse edge is meaningful, the seed inserts it as its own row.

    The unique constraint on ``(source, related, type)`` makes the seed
    idempotent — re-runs skip rows already present, so hand-curated edits
    survive re-seeding. The CHECK constraint blocks self-references outright;
    a service should never appear in its own related list.
    """

    __tablename__ = "service_relationships"

    __table_args__ = (
        UniqueConstraint(
            "source_service_id",
            "related_service_id",
            "relationship_type",
            name="uix_service_relationship",
        ),
        CheckConstraint(
            "source_service_id <> related_service_id",
            name="no_self_reference",
        ),
        # Read path is always "give me the N edges of type T for source S in
        # sort order" — composite index in that key order lets it be an
        # index-only scan.
        Index(
            "idx_service_relationships_source",
            "source_service_id",
            "relationship_type",
            "sort_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False,
    )
    related_service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Lower first — the seed writes 0, 1, 2, ... in the order it decided the
    # picks. Ties broken by service name to keep re-runs deterministic.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
