"""SVC-430: the admin catalog surface (/admin/catalog/*).

Money goes out as JSON numbers, never Decimal strings. ``price_unit`` and
``icon_key`` come in as plain strings and are checked by the service layer, so a
bad value is a 422 ``CATALOG_INVALID`` naming the field rather than FastAPI's
generic list. Neither PATCH body has ``slug`` or ``is_active``, and both refuse
unknown fields: the slug never changes, and only deactivate / activate switch a
row, so the active-bookings warning can't be skipped.
"""

import uuid
from datetime import datetime
from decimal import Decimal

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


class AdminServiceVariantRow(BaseModel):
    """Read-only here: variants are edited elsewhere."""

    id: uuid.UUID
    label: str
    subcategory: str | None
    base_price: float
    duration_minutes: int | None
    sort_order: int
    is_active: bool


class AdminServiceDetail(AdminCatalogServiceRow):
    """The tree row plus the description and every variant, active or not."""

    description: str | None
    created_at: datetime
    updated_at: datetime
    variants: list[AdminServiceVariantRow]


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
