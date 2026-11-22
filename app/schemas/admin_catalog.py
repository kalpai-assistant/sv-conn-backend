"""SVC-430: the admin catalog surface (/admin/catalog/*).

Money goes out as JSON numbers, never Decimal strings. ``price_unit`` and
``icon_key`` come in as plain strings and are checked by the service layer, so a
bad value is a 422 ``CATALOG_INVALID`` naming the field rather than FastAPI's
generic list. Neither PATCH body has ``slug`` or ``is_active``, and both refuse
unknown fields: the slug never changes, and only deactivate / activate switch a
row, so the active-bookings warning can't be skipped.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool

from app.models.price_unit import PriceUnit

# -- reads --------------------------------------------------------------------


class AdminCatalogServiceRow(BaseModel):
    """A service in the tree. ``active_variant_count`` counts the variants a
    customer can pick; at 0 the service books at ``base_price``.
    ``active_bookings`` counts bookings that are requested, accepted, en route or
    in progress."""

    id: uuid.UUID
    category_id: uuid.UUID
    name: str
    price_unit: PriceUnit
    min_qty: int | None
    base_price: float
    duration_minutes: int
    icon_key: str | None
    is_active: bool
    active_variant_count: int
    active_bookings: int


class AdminCatalogCategoryNode(BaseModel):
    """A category in the tree. An L1 (``parent_id`` null) lists its
    sub-categories in ``children``; a category holding services directly (an L2,
    or an L1 like Miscellaneous) lists them in ``services``. Inactive rows are
    included."""

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    icon_key: str | None
    sort_order: int
    is_active: bool
    parent_id: uuid.UUID | None
    children: list["AdminCatalogCategoryNode"]
    services: list[AdminCatalogServiceRow]


class AdminCatalogTree(BaseModel):
    """Every category and service, active or not. Categories are ordered by
    ``sort_order`` then name among their siblings; services by name."""

    categories: list[AdminCatalogCategoryNode]


class AdminCategoryDetail(BaseModel):
    """One category with what it holds. Deactivate is refused while
    ``active_service_count`` or ``active_subcategory_count`` is above 0."""

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    icon_key: str | None
    sort_order: int
    is_active: bool
    parent_id: uuid.UUID | None
    created_at: datetime
    subcategory_count: int
    active_subcategory_count: int
    service_count: int
    active_service_count: int


class AdminPendingCommissionChange(BaseModel):
    """A commission change scheduled on a variant and not yet applied (SVC-486).
    It takes effect at 00:00 IST on ``effective_from``."""

    id: uuid.UUID
    new_pct: float
    effective_from: date
    created_at: datetime


class AdminServiceVariantRow(BaseModel):
    """One variant, active or not, edited through ``/variants`` (SVC-481).

    ``platform_commission + provider_payout == base_price`` always.
    ``commission_pct`` is the commission's share of the price rounded half up to
    2 dp, the value the editor shows and a PATCH may send back unchanged.
    ``gst_percent`` is the customer's GST and is read-only."""

    id: uuid.UUID
    label: str
    subcategory: str | None
    base_price: float
    duration_minutes: int | None
    sort_order: int
    is_active: bool
    platform_commission: float
    provider_payout: float
    commission_pct: float
    gst_percent: float
    # SVC-486: null when nothing is scheduled. At most one per variant.
    pending_commission_change: AdminPendingCommissionChange | None


class AdminServiceDetail(AdminCatalogServiceRow):
    """The tree row plus the description and every variant, active or not."""

    description: str | None
    created_at: datetime
    updated_at: datetime
    variants: list[AdminServiceVariantRow]
    # SVC-481: 2+ variant rows (any state). base_price then follows the cheapest
    # active variant and can't be set directly.
    priced_per_variant: bool


# -- writes -------------------------------------------------------------------


class AdminCategoryCreate(BaseModel):
    """A new category. The slug is made from the name and never changes.
    ``parent_id`` null makes an L1 (home tile); set, an L2 under that L1."""

    model_config = ConfigDict(extra="forbid")

    name: str
    parent_id: uuid.UUID | None = None
    sort_order: int = 0
    icon_key: str | None = None
    is_active: StrictBool = True


class AdminCategoryUpdate(BaseModel):
    """Any subset. ``parent_id: null`` makes it an L1 and ``icon_key: null``
    clears the icon; ``null`` is refused for ``name`` and ``sort_order``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    parent_id: uuid.UUID | None = None
    sort_order: int | None = None
    icon_key: str | None = None


class AdminServiceCreate(BaseModel):
    """A new service. ``min_qty`` is required for PER_SQFT / PER_HOUR and must be
    left out (or null) for PER_VISIT / PER_ITEM. ``description`` is markdown,
    stored as sent."""

    model_config = ConfigDict(extra="forbid")

    category_id: uuid.UUID
    name: str
    description: str | None = None
    base_price: Decimal
    duration_minutes: int = 60
    price_unit: str
    min_qty: int | None = None
    icon_key: str | None = None
    is_active: StrictBool = True


class AdminServiceUpdate(BaseModel):
    """Any subset. ``null`` clears ``description``, ``min_qty`` or ``icon_key``
    and is refused on the others. Moving off PER_SQFT / PER_HOUR needs
    ``min_qty: null`` in the same save."""

    model_config = ConfigDict(extra="forbid")

    category_id: uuid.UUID | None = None
    name: str | None = None
    description: str | None = None
    base_price: Decimal | None = None
    duration_minutes: int | None = None
    price_unit: str | None = None
    min_qty: int | None = None
    icon_key: str | None = None


class AdminServiceDeactivate(BaseModel):
    """``confirm: true`` deactivates even while bookings are in flight. They
    carry on; only new bookings are refused."""

    model_config = ConfigDict(extra="forbid")

    confirm: StrictBool = False


# -- variants (SVC-481) ---------------------------------------------------------


class AdminVariantCreate(BaseModel):
    """A new variant. ``commission_pct`` is the platform's percent of the price;
    the amounts are stored. ``duration_minutes`` left out means the service's
    duration applies; ``sort_order`` left out puts it last. ``gst_percent`` is
    copied from the service's first variant: sending it is refused."""

    model_config = ConfigDict(extra="forbid")

    label: str
    base_price: Decimal
    commission_pct: Decimal
    duration_minutes: int | None = None
    sort_order: int | None = None
    # Declared only so that sending it is a CATALOG_INVALID naming the field.
    gst_percent: Any = None


class AdminVariantUpdate(BaseModel):
    """Any subset of the editable fields; ``null`` is refused.

    SVC-486: raising ``commission_pct`` above the current (rounded) percent is
    always a 409 ``COMMISSION_INCREASE_NEEDS_NOTICE``. An increase is scheduled
    instead (``POST .../commission-changes``). A lower percent still applies at
    once, and is recorded and notified like a scheduled change. Any change of
    percent while a change is scheduled is a 409 ``COMMISSION_CHANGE_SCHEDULED``.
    ``confirm`` is deprecated and ignored. It is still accepted so an older
    console that sends it doesn't get a 422."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    base_price: Decimal | None = None
    commission_pct: Decimal | None = None
    duration_minutes: int | None = None
    sort_order: int | None = None
    confirm: StrictBool = False
    gst_percent: Any = None


class AdminCommissionChangeCreate(BaseModel):
    """Schedule a change of a variant's commission percent (SVC-486). It takes
    effect at 00:00 IST on ``effective_from``. An increase needs that date at
    least 14 days after today (IST). A decrease can be dated today, and then it
    applies at once."""

    model_config = ConfigDict(extra="forbid")

    new_pct: Decimal
    effective_from: date


class AdminCommissionChange(BaseModel):
    """One commission change. ``status`` is ``scheduled``, ``applied`` or
    ``cancelled``. ``old_pct`` is the percent when it was scheduled. Once it is
    applied, the amounts it set are what the variant row shows."""

    id: uuid.UUID
    service_variant_id: uuid.UUID
    old_pct: float
    new_pct: float
    effective_from: date
    status: str
    created_at: datetime
    applied_at: datetime | None
    cancelled_at: datetime | None


class AdminVariantPriceImpact(BaseModel):
    """What a price change would do to the recommended range, for the confirm
    dialog. Counts SPs with an active price on the variant (KYC verified, active
    account), in every area. ``outside_now`` / ``outside_after``: outside the
    range at the current / proposed price. ``moving_out`` / ``moving_in``: in
    range now and outside after, and the reverse. A category with no range
    (not sensitive) has all four at 0."""

    sp_prices: int
    outside_now: int
    outside_after: int
    moving_out: int
    moving_in: int
