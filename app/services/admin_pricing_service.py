"""SVC-428: reads and audited edits of the marketplace pricing config.

Per-category fields live on ``categories``, global knobs in ``pricing_config``.
Every save that changes something writes one ``admin_actions`` row whose
before/after hold only the changed fields. A save that changes nothing writes
no row.

A refused value is a 422 ``PRICING_INVALID`` with ``details.field`` naming the
field, so the console can show it under the cell.
"""

import math
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException
from app.models.category import Category
from app.models.pricing_config import PRICING_CONFIG_KEYS, RANKING_WEIGHT_KEYS, PricingConfig
from app.schemas.admin_pricing import CategoryPricingRow, CategoryPricingUpdate, PricingConfigRow
from app.services.admin_audit import AuditActor, audit_admin_action

CATEGORY_PRICING_FIELDS = (
    "is_sensitive",
    "reference_price",
    "bounds_pct",
    "platform_commission_pct",
    "platform_commission_min",
    "commission_gst_pct",
)
# Only these two may be set to null.
_NULLABLE_FIELDS = frozenset({"reference_price", "bounds_pct"})
_HUNDRED = Decimal("100")
# numeric(10,2) holds up to 99,999,999.99.
_MONEY_LIMIT = Decimal("100000000")
WEIGHT_SUM_TOLERANCE = 0.01
# Room for any sane window or TTL, and still a 32-bit int for whoever reads it.
_INT_CONFIG_MAX = 2_147_483_647


class PricingInvalid(AppException):
    def __init__(self, field: str, message: str, **extra: Any):
        super().__init__(
            message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="PRICING_INVALID",
            details={"field": field, **extra},
        )


class CategoryNotFound(AppException):
    def __init__(self):
        super().__init__(
            "Category not found",
            status_code=status.HTTP_404_NOT_FOUND,
            code="CATEGORY_NOT_FOUND",
        )


class PricingConfigNotFound(AppException):
    def __init__(self, key: str):
        super().__init__(
            f"Unknown pricing config key {key!r}",
            status_code=status.HTTP_404_NOT_FOUND,
            code="PRICING_CONFIG_NOT_FOUND",
        )


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _category_row(category: Category, parent_name: str | None) -> CategoryPricingRow:
    return CategoryPricingRow(
        id=category.id,
        name=category.name,
        slug=category.slug,
        parent_id=category.parent_id,
        parent_name=parent_name,
        sort_order=category.sort_order or 0,
        is_active=bool(category.is_active),
        is_sensitive=category.is_sensitive,
        reference_price=_money(category.reference_price),
        bounds_pct=_money(category.bounds_pct),
        platform_commission_pct=float(category.platform_commission_pct),
        platform_commission_min=float(category.platform_commission_min),
        commission_gst_pct=float(category.commission_gst_pct),
    )


def _check_decimal(field: str, value: Decimal) -> None:
    if not value.is_finite():
        raise PricingInvalid(field, f"{field} must be a number")
    if value != value.quantize(Decimal("0.01")):
        raise PricingInvalid(field, f"{field} takes at most 2 decimal places")


def _validate_category(values: dict[str, Any], sent: dict[str, Any]) -> None:
    """``values`` is the row as it would be saved, ``sent`` what the body set."""
    for field, value in sent.items():
        if value is None and field not in _NULLABLE_FIELDS:
            raise PricingInvalid(field, f"{field} can't be empty")
        if isinstance(value, Decimal):
            _check_decimal(field, value)

    for field in ("platform_commission_pct", "commission_gst_pct"):
        if not Decimal(0) <= values[field] <= _HUNDRED:
            raise PricingInvalid(field, f"{field} must be between 0 and 100")
    if not Decimal(0) <= values["platform_commission_min"] < _MONEY_LIMIT:
        raise PricingInvalid("platform_commission_min", "platform_commission_min must be 0 or more")
    reference = values["reference_price"]
    if reference is not None and not Decimal(0) < reference < _MONEY_LIMIT:
        raise PricingInvalid("reference_price", "reference_price must be more than 0, or empty")

    bounds = values["bounds_pct"]
    if values["is_sensitive"]:
        if bounds is None:
            raise PricingInvalid("bounds_pct", "A sensitive category needs bounds_pct")
        if not Decimal(0) < bounds <= _HUNDRED:
            raise PricingInvalid("bounds_pct", "bounds_pct must be more than 0 and at most 100")
    elif sent.get("bounds_pct") is not None:
        raise PricingInvalid("bounds_pct", "bounds_pct applies only to a sensitive category")


def _is_number(value: Any) -> bool:
    # bool is an int subclass, and True is not a weight.
    return isinstance(value, int | float) and not isinstance(value, bool)


def _validate_config(key: str, value: Any) -> Any:
    """The value to store for ``key``, or a 422."""
    if key == "ranking_weights":
        if not isinstance(value, dict):
            raise PricingInvalid("value", "ranking_weights must be an object of weights")
        missing = [k for k in RANKING_WEIGHT_KEYS if k not in value]
        unexpected = sorted(k for k in value if k not in RANKING_WEIGHT_KEYS)
        if missing or unexpected:
            raise PricingInvalid(
                "value",
                f"ranking_weights needs exactly {', '.join(RANKING_WEIGHT_KEYS)}",
                missing=missing,
                unexpected=unexpected,
            )
        for weight in RANKING_WEIGHT_KEYS:
            v = value[weight]
            if not _is_number(v) or not math.isfinite(v) or not 0 <= v <= 1:
                raise PricingInvalid(
                    "value", f"{weight} must be a number from 0 to 1", weight=weight
                )
        total = sum(float(value[w]) for w in RANKING_WEIGHT_KEYS)
        # The epsilon keeps 0.99 / 1.01 in: float sums land a hair outside.
        if abs(total - 1) > WEIGHT_SUM_TOLERANCE + 1e-9:
            raise PricingInvalid(
                "value",
                f"ranking_weights must sum to 1.00 (now {total:.2f})",
                sum=round(total, 4),
            )
        return {w: float(value[w]) for w in RANKING_WEIGHT_KEYS}

    # boost_window_days, boost_window_jobs, sheet_ttl_seconds: a whole number.
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= _INT_CONFIG_MAX:
        raise PricingInvalid("value", f"{key} must be a whole number greater than 0")
    return value


def _config_row(row: PricingConfig) -> PricingConfigRow:
    return PricingConfigRow(
        key=row.key, value=row.value_json, updated_at=row.updated_at, updated_by=row.updated_by
    )


class AdminPricingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # -- categories ----------------------------------------------------------

    async def list_categories(self) -> list[CategoryPricingRow]:
        """Every category, active or not: each L1 by sort_order and name,
        followed by its L2s in the same order."""
        categories = list(await self.db.scalars(select(Category)))
        by_id = {c.id: c for c in categories}

        def order(c: Category) -> tuple:
            own = (c.sort_order or 0, c.name, str(c.id))
            parent = by_id.get(c.parent_id) if c.parent_id else None
            if parent is None:
                return (*own, 0, 0, "", "")
            return (parent.sort_order or 0, parent.name, str(parent.id), 1, *own)

        return [
            _category_row(c, by_id[c.parent_id].name if c.parent_id in by_id else None)
            for c in sorted(categories, key=order)
        ]

    async def update_category(
        self, actor: AuditActor, category_id: uuid.UUID, body: CategoryPricingUpdate
    ) -> CategoryPricingRow:
        sent = {f: getattr(body, f) for f in body.model_fields_set}
        if not sent:
            raise PricingInvalid("body", "Send at least one field to change")

        # Locked, so two cells saved at once can't each pass the sensitive/bounds
        # rule against a row the other is changing.
        category = await self.db.scalar(
            select(Category)
            .where(Category.id == category_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if category is None:
            raise CategoryNotFound()

        old = {f: getattr(category, f) for f in CATEGORY_PRICING_FIELDS}
        new = {**old, **sent}
        _validate_category(new, sent)
        if not new["is_sensitive"]:
            new["bounds_pct"] = None

        changed = [f for f in CATEGORY_PRICING_FIELDS if new[f] != old[f]]
        if changed:
            for field in changed:
                setattr(category, field, new[field])
            await self.db.flush()
            await audit_admin_action(
                self.db,
                actor,
                action="pricing.category_update",
                target_type="category",
                target_id=category.id,
                before={f: old[f] for f in changed},
                after={f: new[f] for f in changed},
            )

        parent_name = None
        if category.parent_id is not None:
            parent = await self.db.get(Category, category.parent_id)
            parent_name = parent.name if parent else None
        return _category_row(category, parent_name)

    # -- global config -------------------------------------------------------

    async def list_config(self) -> list[PricingConfigRow]:
        rows = list(await self.db.scalars(select(PricingConfig)))
        rank = {k: i for i, k in enumerate(PRICING_CONFIG_KEYS)}
        rows.sort(key=lambda r: (rank.get(r.key, len(rank)), r.key))
        return [_config_row(r) for r in rows]

    async def update_config(self, actor: AuditActor, key: str, value: Any) -> PricingConfigRow:
        row = None
        if key in PRICING_CONFIG_KEYS:
            row = await self.db.scalar(
                select(PricingConfig)
                .where(PricingConfig.key == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if row is None:
            raise PricingConfigNotFound(key)

        stored = _validate_config(key, value)
        old = row.value_json
        if stored != old:
            row.value_json = stored
            row.updated_at = datetime.now(UTC)
            row.updated_by = actor.admin_id
            await self.db.flush()
            await audit_admin_action(
                self.db,
                actor,
                action="pricing.config_update",
                target_type="pricing_config",
                target_id=key,
                before={"value_json": old},
                after={"value_json": stored},
            )
        return _config_row(row)
