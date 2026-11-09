"""Virtual-number pool for call masking (SVC-448).

The Exotel numbers the platform owns and rents out to bridge customer/SP
calls. One row per number; ``status`` controls whether the allocator may
pick it. The pool is manually managed from the admin console (SVC-454) —
new numbers come in via Exotel onboarding (SVC-447) and are added here.

Design decisions (see ticket SVC-448):

* UUID PK, not bigint, to match the rest of the SVC schema.
* ``e164`` is the full E.164 string (``+918065012345``) stored as varchar(16)
  — one format everywhere, no split country/number pair, no parsing on
  every webhook.
* ``provider`` is already an enum even though only ``exotel`` exists today
  so a future provider (Knowlarity, Plivo, etc.) is a migration that adds
  an enum value, not a shape change.
* ``status`` is a tri-state (not a bool) so a number can be taken out of
  the pool temporarily (``disabled`` — Exotel maintenance, pending DLT
  resubmit) without losing the row that ``call_mappings`` / ``call_records``
  FK to via ``virtual_number_id``.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class VirtualNumberProvider(str, enum.Enum):
    EXOTEL = "exotel"


class VirtualNumberStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class VirtualNumberPool(Base):
    """A single virtual number the allocator can rent out.

    ``active`` means eligible for pick. ``disabled`` is a soft out-of-rotation
    (Exotel issue, DLT resubmit) — flip back to ``active`` to resume. ``retired``
    is permanent: the number is gone from the provider. Historical mappings and
    records still point here because the FK is deliberately not cascade-deleted.
    """

    __tablename__ = "virtual_number_pool"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Exotel number in full E.164: `+918065012345`. Unique because a number
    # cannot be in the pool twice, and the webhook reconciler keys off it.
    e164: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    provider: Mapped[VirtualNumberProvider] = mapped_column(
        Enum(
            VirtualNumberProvider,
            name="virtualnumberprovider",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=VirtualNumberProvider.EXOTEL.value,
    )
    status: Mapped[VirtualNumberStatus] = mapped_column(
        Enum(
            VirtualNumberStatus,
            name="virtualnumberstatus",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=VirtualNumberStatus.ACTIVE.value,
        index=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Free-form admin notes: why disabled, contract id, pricing plan. Not
    # surfaced to customers / SPs anywhere.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
