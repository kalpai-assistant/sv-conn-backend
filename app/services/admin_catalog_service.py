"""SVC-430: admin reads and audited writes of the service catalog.

Categories are two levels: an L1 (``parent_id`` NULL) is a home tile, an L2 sits
under an L1. A category holds sub-categories or services, never both. The public
catalog routes (``CatalogService``) are not touched; this module only creates,
edits and switches rows.

* A slug is made from the name at create and never changes: the consumer app keys
  its icons, tile images and ``/category/$slug`` route on it.
* Nothing is deleted. Deactivate / activate are the only way ``is_active`` moves,
  so a PATCH can't skip the active-bookings warning.
* The structure rules count rows whether they're active or not. An active row
  can't sit under an inactive one, checked on create, move and activate.
* Every save that changes something writes one ``admin_actions`` row with only
  the fields it changed. A save that changes nothing writes none.

Concurrency: a write locks its own row, then the category it puts something
under (``FOR UPDATE``), always child before parent. Deactivating a category takes
the same lock on it before counting, so a service or sub-category can't be added
under it between the count and the switch.

A refused value is a 422 ``CATALOG_INVALID`` with ``details.field`` naming the
field, as SVC-429's ``COUPON_INVALID`` does. On PATCH a field's own rule runs only
when that field is sent, so an old row with a value the rules now refuse (a seeded
service at base_price 0.00) can still be renamed. Cross-field rules run on the row
as it would be saved.
"""

import re
import uuid
from decimal import Decimal
from typing import Any

from fastapi import status
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, ServiceNotFound
from app.models.booking import Booking
from app.models.booking_item import BookingItem
from app.models.category import Category
from app.models.price_unit import UNITS_WITH_MIN_QTY, PriceUnit
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.schemas.admin_catalog import (
    AdminCatalogCategoryNode,
    AdminCatalogServiceRow,
    AdminCatalogTree,
    AdminCategoryCreate,
    AdminCategoryDetail,
    AdminCategoryUpdate,
    AdminServiceCreate,
    AdminServiceDetail,
    AdminServiceUpdate,
    AdminServiceVariantRow,
)
from app.seed_data.catalog_icons import CATALOG_ICON_KEYS
from app.services.account_service import _LIVE_STATUSES
from app.services.admin_audit import AuditActor, audit_admin_action, snapshot
from app.utils.money import to_decimal

# What a category create records. SVC-428's pricing fields are on the same
# record type but aren't set here.
CATEGORY_AUDIT_FIELDS = ("name", "slug", "icon_key", "sort_order", "is_active", "parent_id")
CATEGORY_EDITABLE = ("name", "parent_id", "sort_order", "icon_key")
_CATEGORY_NULLABLE = frozenset({"parent_id", "icon_key"})
SERVICE_EDITABLE = (
    "category_id",
    "name",
    "description",
    "base_price",
    "duration_minutes",
    "price_unit",
    "min_qty",
    "icon_key",
)
_SERVICE_NULLABLE = frozenset({"description", "min_qty", "icon_key"})

CATEGORY_NAME_LENGTH = (2, 100)
SERVICE_NAME_LENGTH = (3, 200)
DESCRIPTION_MAX = 2000
SORT_ORDER_RANGE = (0, 9999)
DURATION_RANGE = (15, 480)
MIN_QTY_RANGE = (1, 100_000)
PRICE_MAX = Decimal("1000000")
_ICON_KEYS = frozenset(CATALOG_ICON_KEYS)


class CatalogInvalid(AppException):
    def __init__(self, field: str, message: str):
        super().__init__(
            message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="CATALOG_INVALID",
            details={"field": field},
        )


class CatalogNameTaken(AppException):
    def __init__(self, message: str):
        super().__init__(
            message,
            status_code=status.HTTP_409_CONFLICT,
            code="CATALOG_NAME_TAKEN",
            details={"field": "name"},
        )


class CategoryNotFound(AppException):
    def __init__(self):
        super().__init__(
            "Category not found", status_code=status.HTTP_404_NOT_FOUND, code="CATEGORY_NOT_FOUND"
        )


class CategoryNotEmpty(AppException):
    def __init__(self, active_services: int, active_subcategories: int):
        super().__init__(
            "Deactivate or move this category's active services and sub-categories first",
            status_code=status.HTTP_409_CONFLICT,
            code="CATEGORY_NOT_EMPTY",
            details={
                "active_services": active_services,
                "active_subcategories": active_subcategories,
            },
        )


class ServiceHasActiveBookings(AppException):
    def __init__(self, active_bookings: int):
        super().__init__(
            f"This service has {active_bookings} booking(s) in progress. They will carry on; "
            "send confirm: true to deactivate anyway",
            status_code=status.HTTP_409_CONFLICT,
            code="SERVICE_HAS_ACTIVE_BOOKINGS",
            details={"active_bookings": active_bookings},
        )


# -- value rules ----------------------------------------------------------------


def slugify(name: str) -> str:
    """Lowercase, every run of characters outside [a-z0-9] becomes one dash,
    no dash at either end. ``"Salon & Spa"`` → ``"salon-spa"``."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _name(field: str, raw: str, bounds: tuple[int, int]) -> str:
    name = raw.strip()
    low, high = bounds
    if not low <= len(name) <= high:
        raise CatalogInvalid(field, f"Name must be {low} to {high} characters")
    return name


def _in_range(field: str, value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    if not low <= value <= high:
        raise CatalogInvalid(field, f"{field} must be {low} to {high}")
    return value


def _icon_key(value: str | None) -> str | None:
    if value is not None and value not in _ICON_KEYS:
        raise CatalogInvalid("icon_key", "Pick an icon from the list")
    return value


def _description(value: str | None) -> str | None:
    """Markdown, stored as sent. Blank means no description."""
    if value is None or not value.strip():
        return None
    if len(value) > DESCRIPTION_MAX:
        raise CatalogInvalid(
            "description", f"Description can be at most {DESCRIPTION_MAX} characters"
        )
    return value


def _base_price(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise CatalogInvalid("base_price", "base_price must be a number")
    if value != value.quantize(Decimal("0.01")):
        raise CatalogInvalid("base_price", "base_price takes at most 2 decimal places")
    if not Decimal("0") < value <= PRICE_MAX:
        raise CatalogInvalid("base_price", "base_price must be more than 0 and at most 1000000")
    return to_decimal(value)


def _price_unit(value: str) -> str:
    try:
        return PriceUnit(value.strip().upper()).value
    except ValueError:
        units = ", ".join(u.value for u in PriceUnit)
        raise CatalogInvalid("price_unit", f"price_unit must be one of {units}") from None


def _unit_and_qty(price_unit: str, min_qty: int | None) -> None:
    """min_qty is required for PER_SQFT / PER_HOUR and refused otherwise, never
    silently dropped."""
    needs_qty = PriceUnit(price_unit) in UNITS_WITH_MIN_QTY
    if needs_qty and min_qty is None:
        raise CatalogInvalid("min_qty", f"min_qty is required for {price_unit}")
    if not needs_qty and min_qty is not None:
        raise CatalogInvalid(
            "min_qty", f"{price_unit} takes no min_qty; send min_qty: null to clear it"
        )


def _refuse_nulls(sent: dict[str, Any], nullable: frozenset[str]) -> None:
    for field, value in sent.items():
        if value is None and field not in nullable:
            raise CatalogInvalid(field, f"{field} can't be empty")


# -- queries --------------------------------------------------------------------


def _active_variant_counts():
    return (
        select(ServiceVariant.service_id, func.count())
        .where(ServiceVariant.is_active.is_(True))
        .group_by(ServiceVariant.service_id)
    )


def _active_booking_counts():
    """Bookings requested, accepted, en route or in progress, per service.
    ``_LIVE_STATUSES`` is account deletion's definition of an active booking."""
    return (
        select(BookingItem.service_id, func.count(distinct(Booking.id)))
        .join(Booking, Booking.id == BookingItem.booking_id)
        .where(Booking.status.in_(_LIVE_STATUSES))
        .group_by(BookingItem.service_id)
    )


_SERVICE_ROW_COLUMNS = (
    Service.id,
    Service.category_id,
    Service.name,
    Service.price_unit,
    Service.min_qty,
    Service.base_price,
    Service.duration_minutes,
    Service.icon_key,
    Service.is_active,
)


def _service_row(row, variants: dict, bookings: dict) -> AdminCatalogServiceRow:
    return AdminCatalogServiceRow(
        id=row.id,
        category_id=row.category_id,
        name=row.name,
        price_unit=row.price_unit,
        min_qty=row.min_qty,
        base_price=float(row.base_price),
        duration_minutes=row.duration_minutes,
        icon_key=row.icon_key,
        is_active=row.is_active,
        active_variant_count=variants.get(row.id, 0),
        active_bookings=bookings.get(row.id, 0),
    )


# -- the service ----------------------------------------------------------------


class AdminCatalogService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # -- reads ------------------------------------------------------------------

    async def tree(self) -> AdminCatalogTree:
        """Four statements whatever the catalog's size: categories, services,
        and the two counts grouped by service."""
        categories = (
            await self.db.scalars(
                select(Category).order_by(
                    Category.sort_order, func.lower(Category.name), Category.id
                )
            )
        ).all()
        services = (
            await self.db.execute(
                select(*_SERVICE_ROW_COLUMNS).order_by(func.lower(Service.name), Service.id)
            )
        ).all()
        variants = dict((await self.db.execute(_active_variant_counts())).all())
        bookings = dict((await self.db.execute(_active_booking_counts())).all())

        nodes = {
            c.id: AdminCatalogCategoryNode(
                id=c.id,
                name=c.name,
                slug=c.slug,
                description=c.description,
                icon_key=c.icon_key,
                sort_order=c.sort_order,
                is_active=c.is_active,
                parent_id=c.parent_id,
                children=[],
                services=[],
            )
            for c in categories
        }
        roots = []
        for c in categories:
            parent = nodes.get(c.parent_id) if c.parent_id is not None else None
            (parent.children if parent is not None else roots).append(nodes[c.id])
        for row in services:
            nodes[row.category_id].services.append(_service_row(row, variants, bookings))
        return AdminCatalogTree(categories=roots)

    async def category_detail(self, category_id: uuid.UUID) -> AdminCategoryDetail:
        category = await self.db.get(Category, category_id, populate_existing=True)
        if category is None:
            raise CategoryNotFound()
        kids, active_kids = (
            await self.db.execute(
                select(func.count(), func.count().filter(Category.is_active.is_(True))).where(
                    Category.parent_id == category_id
                )
            )
        ).one()
        services, active_services = await self._service_counts(category_id)
        return AdminCategoryDetail(
            id=category.id,
            name=category.name,
            slug=category.slug,
            description=category.description,
            icon_key=category.icon_key,
            sort_order=category.sort_order,
            is_active=category.is_active,
            parent_id=category.parent_id,
            created_at=category.created_at,
            subcategory_count=kids,
            active_subcategory_count=active_kids,
            service_count=services,
            active_service_count=active_services,
        )

    async def service_detail(self, service_id: uuid.UUID) -> AdminServiceDetail:
        service = await self.db.get(Service, service_id, populate_existing=True)
        if service is None:
            raise ServiceNotFound(service_id)
        variant_rows = (
            await self.db.scalars(
                select(ServiceVariant)
                .where(ServiceVariant.service_id == service_id)
                .order_by(ServiceVariant.sort_order, ServiceVariant.id)
            )
        ).all()
        active_bookings = await self._active_bookings(service_id)
        return AdminServiceDetail(
            id=service.id,
            category_id=service.category_id,
            name=service.name,
            price_unit=service.price_unit,
            min_qty=service.min_qty,
            base_price=float(service.base_price),
            duration_minutes=service.duration_minutes,
            icon_key=service.icon_key,
            is_active=service.is_active,
            active_variant_count=sum(1 for v in variant_rows if v.is_active),
            active_bookings=active_bookings,
            description=service.description,
            created_at=service.created_at,
            updated_at=service.updated_at,
            variants=[
                AdminServiceVariantRow(
                    id=v.id,
                    label=v.label,
                    subcategory=v.subcategory,
                    base_price=float(v.base_price),
                    duration_minutes=v.duration_minutes,
                    sort_order=v.sort_order,
                    is_active=v.is_active,
                )
                for v in variant_rows
            ],
        )

    # -- category writes --------------------------------------------------------

    async def create_category(
        self, actor: AuditActor, body: AdminCategoryCreate
    ) -> AdminCategoryDetail:
        name = _name("name", body.name, CATEGORY_NAME_LENGTH)
        slug = slugify(name)
        if not slug:
            raise CatalogInvalid("name", "Name needs at least one letter or digit")
        sort_order = _in_range("sort_order", body.sort_order, SORT_ORDER_RANGE)
        icon_key = _icon_key(body.icon_key)
        if body.parent_id is not None:
            await self._check_parent(body.parent_id, child=None, active=body.is_active)
        await self._check_category_name_free(name, slug=slug, exclude=None)

        category = Category(
            name=name,
            slug=slug,
            icon_key=icon_key,
            sort_order=sort_order,
            is_active=body.is_active,
            parent_id=body.parent_id,
        )
        await self._flush_category(category)
        await audit_admin_action(
            self.db,
            actor,
            action="category.create",
            target_type="category",
            target_id=category.id,
            after={f: getattr(category, f) for f in CATEGORY_AUDIT_FIELDS},
        )
        return await self.category_detail(category.id)

    async def update_category(
        self, actor: AuditActor, category_id: uuid.UUID, body: AdminCategoryUpdate
    ) -> AdminCategoryDetail:
        category = await self._lock_category(category_id)
        if category is None:
            raise CategoryNotFound()
        sent = {f: getattr(body, f) for f in body.model_fields_set}
        if not sent:
            raise CatalogInvalid("body", "Send at least one field to change")
        _refuse_nulls(sent, _CATEGORY_NULLABLE)

        old = {f: getattr(category, f) for f in CATEGORY_EDITABLE}
        new = dict(old)
        if "name" in sent:
            new["name"] = _name("name", sent["name"], CATEGORY_NAME_LENGTH)
        if "sort_order" in sent:
            new["sort_order"] = _in_range("sort_order", sent["sort_order"], SORT_ORDER_RANGE)
        if "icon_key" in sent:
            new["icon_key"] = _icon_key(sent["icon_key"])
        if "parent_id" in sent:
            new["parent_id"] = sent["parent_id"]

        changed = [f for f in CATEGORY_EDITABLE if new[f] != old[f]]
        if "parent_id" in changed and new["parent_id"] is not None:
            await self._check_parent(new["parent_id"], child=category, active=category.is_active)
        if "name" in changed:
            # The slug stays, so only the name has to be free.
            await self._check_category_name_free(new["name"], slug=None, exclude=category.id)

        if changed:
            for field in changed:
                setattr(category, field, new[field])
            await self._flush_category(category)
            await audit_admin_action(
                self.db,
                actor,
                action="category.update",
                target_type="category",
                target_id=category.id,
                before={f: old[f] for f in changed},
                after={f: new[f] for f in changed},
            )
        return await self.category_detail(category.id)

    async def set_category_active(
        self, actor: AuditActor, category_id: uuid.UUID, active: bool
    ) -> AdminCategoryDetail:
        """Deactivate: refused while it holds an active service or sub-category.
        Activate: refused while its parent is inactive. Already in that state:
        200, no audit row."""
        category = await self._lock_category(category_id)
        if category is None:
            raise CategoryNotFound()
        if category.is_active != active:
            if active:
                if category.parent_id is not None:
                    parent = await self._lock_category(category.parent_id)
                    if parent is not None and not parent.is_active:
                        raise CatalogInvalid(
                            "parent_id", "Its parent category is inactive; activate that first"
                        )
            else:
                active_kids = await self.db.scalar(
                    select(func.count()).where(
                        Category.parent_id == category.id, Category.is_active.is_(True)
                    )
                )
                _, active_services = await self._service_counts(category.id)
                if active_kids or active_services:
                    raise CategoryNotEmpty(active_services, active_kids)
            category.is_active = active
            await self.db.flush()
            await audit_admin_action(
                self.db,
                actor,
                action="category.activate" if active else "category.deactivate",
                target_type="category",
                target_id=category.id,
                before={"is_active": not active},
                after={"is_active": active},
            )
        return await self.category_detail(category.id)

    # -- service writes ---------------------------------------------------------

    async def create_service(
        self, actor: AuditActor, body: AdminServiceCreate
    ) -> AdminServiceDetail:
        name = _name("name", body.name, SERVICE_NAME_LENGTH)
        description = _description(body.description)
        base_price = _base_price(body.base_price)
        duration = _in_range("duration_minutes", body.duration_minutes, DURATION_RANGE)
        price_unit = _price_unit(body.price_unit)
        min_qty = (
            _in_range("min_qty", body.min_qty, MIN_QTY_RANGE) if body.min_qty is not None else None
        )
        _unit_and_qty(price_unit, min_qty)
        icon_key = _icon_key(body.icon_key)
        await self._check_service_category(body.category_id, active=body.is_active)
        await self._check_service_name_free(name, body.category_id, exclude=None)

        service = Service(
            category_id=body.category_id,
            name=name,
            description=description,
            base_price=base_price,
            duration_minutes=duration,
            price_unit=price_unit,
            min_qty=min_qty,
            icon_key=icon_key,
            is_active=body.is_active,
        )
        self.db.add(service)
        await self.db.flush()
        await audit_admin_action(
            self.db,
            actor,
            action="service.create",
            target_type="service",
            target_id=service.id,
            after=snapshot("service", service),
        )
        return await self.service_detail(service.id)

    async def update_service(
        self, actor: AuditActor, service_id: uuid.UUID, body: AdminServiceUpdate
    ) -> AdminServiceDetail:
        service = await self._lock_service(service_id)
        sent = {f: getattr(body, f) for f in body.model_fields_set}
        if not sent:
            raise CatalogInvalid("body", "Send at least one field to change")
        _refuse_nulls(sent, _SERVICE_NULLABLE)

        old = {f: getattr(service, f) for f in SERVICE_EDITABLE}
        new = dict(old)
        if "name" in sent:
            new["name"] = _name("name", sent["name"], SERVICE_NAME_LENGTH)
        if "description" in sent:
            new["description"] = _description(sent["description"])
        if "base_price" in sent:
            new["base_price"] = _base_price(sent["base_price"])
        if "duration_minutes" in sent:
            new["duration_minutes"] = _in_range(
                "duration_minutes", sent["duration_minutes"], DURATION_RANGE
            )
        if "price_unit" in sent:
            new["price_unit"] = _price_unit(sent["price_unit"])
        if sent.get("min_qty") is not None:
            new["min_qty"] = _in_range("min_qty", sent["min_qty"], MIN_QTY_RANGE)
        elif "min_qty" in sent:
            new["min_qty"] = None
        if "icon_key" in sent:
            new["icon_key"] = _icon_key(sent["icon_key"])
        if "category_id" in sent:
            new["category_id"] = sent["category_id"]
        _unit_and_qty(new["price_unit"], new["min_qty"])

        changed = [f for f in SERVICE_EDITABLE if new[f] != old[f]]
        if "category_id" in changed:
            await self._check_service_category(new["category_id"], active=service.is_active)
        elif "name" in changed:
            # Serialise against another save naming a service in this category.
            await self._lock_category(service.category_id)
        if "name" in changed or "category_id" in changed:
            await self._check_service_name_free(new["name"], new["category_id"], exclude=service.id)

        if changed:
            for field in changed:
                setattr(service, field, new[field])
            await self.db.flush()
            await audit_admin_action(
                self.db,
                actor,
                action="service.update",
                target_type="service",
                target_id=service.id,
                before={f: old[f] for f in changed},
                after={f: new[f] for f in changed},
            )
        return await self.service_detail(service.id)

    async def deactivate_service(
        self, actor: AuditActor, service_id: uuid.UUID, *, confirm: bool
    ) -> AdminServiceDetail:
        """In-flight bookings carry on either way; booking create refuses an
        inactive service from now on. Already inactive: 200, no audit row."""
        service = await self._lock_service(service_id)
        if service.is_active:
            active_bookings = await self._active_bookings(service.id)
            if active_bookings and not confirm:
                raise ServiceHasActiveBookings(active_bookings)
            service.is_active = False
            await self.db.flush()
            await audit_admin_action(
                self.db,
                actor,
                action="service.deactivate",
                target_type="service",
                target_id=service.id,
                details={"active_bookings": active_bookings, "confirm": confirm},
                before={"is_active": True},
                after={"is_active": False},
            )
        return await self.service_detail(service.id)

    async def activate_service(
        self, actor: AuditActor, service_id: uuid.UUID
    ) -> AdminServiceDetail:
        """Refused while its category is inactive. Already active: 200, no audit row."""
        service = await self._lock_service(service_id)
        if not service.is_active:
            await self._check_service_category(service.category_id, active=True)
            service.is_active = True
            await self.db.flush()
            await audit_admin_action(
                self.db,
                actor,
                action="service.activate",
                target_type="service",
                target_id=service.id,
                before={"is_active": False},
                after={"is_active": True},
            )
        return await self.service_detail(service.id)

    # -- structure checks ---------------------------------------------------------

    async def _check_parent(
        self, parent_id: uuid.UUID, *, child: Category | None, active: bool
    ) -> None:
        """``child`` (None on create) may go under ``parent_id``. Locks the parent."""
        if child is not None and parent_id == child.id:
            raise CatalogInvalid("parent_id", "A category can't be its own parent")
        parent = await self._lock_category(parent_id)
        if parent is None:
            raise CatalogInvalid("parent_id", "No such category")
        if parent.parent_id is not None:
            raise CatalogInvalid(
                "parent_id", "Pick a top-level category: the catalog has two levels"
            )
        if child is not None and await self._has_children(child.id):
            raise CatalogInvalid(
                "parent_id", "This category has sub-categories, so it can't go under another"
            )
        if (await self._service_counts(parent.id))[0]:
            raise CatalogInvalid(
                "parent_id",
                "That category holds services; a category holds sub-categories or services, "
                "not both",
            )
        if active and not parent.is_active:
            raise CatalogInvalid(
                "parent_id", "That category is inactive; an active category can't go under it"
            )

    async def _check_service_category(self, category_id: uuid.UUID, *, active: bool) -> None:
        """A service may go in ``category_id``. Locks the category."""
        category = await self._lock_category(category_id)
        if category is None:
            raise CatalogInvalid("category_id", "No such category")
        if await self._has_children(category.id):
            raise CatalogInvalid(
                "category_id",
                "That category has sub-categories; a category holds sub-categories or "
                "services, not both",
            )
        if active and not category.is_active:
            raise CatalogInvalid(
                "category_id", "That category is inactive; an active service can't go in it"
            )

    async def _check_category_name_free(
        self, name: str, *, slug: str | None, exclude: uuid.UUID | None
    ) -> None:
        clashes = [func.lower(Category.name) == name.lower()]
        if slug is not None:
            clashes.append(Category.slug == slug)
        stmt = select(Category.name, Category.slug).where(or_(*clashes))
        if exclude is not None:
            stmt = stmt.where(Category.id != exclude)
        clash = (await self.db.execute(stmt.limit(1))).first()
        if clash is None:
            return
        if clash.name.lower() == name.lower():
            raise CatalogNameTaken(f"A category named {clash.name} already exists")
        raise CatalogNameTaken(
            f"{name} would get the address {slug}, which {clash.name} already uses"
        )

    async def _check_service_name_free(
        self, name: str, category_id: uuid.UUID, *, exclude: uuid.UUID | None
    ) -> None:
        stmt = select(Service.name).where(
            Service.category_id == category_id, func.lower(Service.name) == name.lower()
        )
        if exclude is not None:
            stmt = stmt.where(Service.id != exclude)
        clash = await self.db.scalar(stmt.limit(1))
        if clash is not None:
            raise CatalogNameTaken(f"This category already has a service named {clash}")

    # -- helpers ----------------------------------------------------------------

    async def _flush_category(self, category: Category) -> None:
        """A savepoint, so a racing save of the same name or slug is a 409 and the
        rest of the transaction survives."""
        try:
            async with self.db.begin_nested():
                self.db.add(category)
                await self.db.flush()
        except IntegrityError:
            raise CatalogNameTaken(f"A category named {category.name} already exists") from None

    async def _lock_category(self, category_id: uuid.UUID) -> Category | None:
        return await self.db.scalar(
            select(Category)
            .where(Category.id == category_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def _lock_service(self, service_id: uuid.UUID) -> Service:
        service = await self.db.scalar(
            select(Service)
            .where(Service.id == service_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if service is None:
            raise ServiceNotFound(service_id)
        return service

    async def _has_children(self, category_id: uuid.UUID) -> bool:
        found = await self.db.scalar(
            select(Category.id).where(Category.parent_id == category_id).limit(1)
        )
        return found is not None

    async def _service_counts(self, category_id: uuid.UUID) -> tuple[int, int]:
        """(all services, active services) in the category."""
        total, active = (
            await self.db.execute(
                select(func.count(), func.count().filter(Service.is_active.is_(True))).where(
                    Service.category_id == category_id
                )
            )
        ).one()
        return total, active

    async def _active_bookings(self, service_id: uuid.UUID) -> int:
        row = (
            await self.db.execute(
                _active_booking_counts().where(BookingItem.service_id == service_id)
            )
        ).first()
        return row[1] if row is not None else 0
