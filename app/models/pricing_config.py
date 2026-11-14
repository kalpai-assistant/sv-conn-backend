import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The keys that exist, in the order GET /admin/pricing/config lists them. The
# SVC-428 migration seeds one row for each; there is no create or delete route.
# Validation per key is in app/services/admin_pricing_service.py.
RANKING_WEIGHT_KEYS = ("rating", "price_vs_ref", "distance", "reliability", "activity")
PRICING_CONFIG_KEYS = (
    "ranking_weights",
    "boost_window_days",
    "boost_window_jobs",
    "sheet_ttl_seconds",
    # SVC-431: the most, in rupees, a support-role admin may refund on one dispute.
    "support_refund_limit",
)
SUPPORT_REFUND_LIMIT_KEY = "support_refund_limit"
SUPPORT_REFUND_LIMIT_DEFAULT = 500


class PricingConfig(Base):
    """Marketplace-wide pricing knobs, one JSON value per key (SVC-428).

    Edited at /admin/pricing/config. Nothing reads them yet: the ranking formula
    and boost window are SVC-406 / SVC-414, the sheet TTL SVC-411.
    ``updated_by`` is NULL on the migration's seed rows.
    """

    __tablename__ = "pricing_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict | list | int | float] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
