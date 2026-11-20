"""SVC-481: the admin variant editor (/admin/catalog/services/{id}/variants*).

A variant is one priced option of a service. Bookings charge the variant, so this
is where an admin sets what a customer pays and how it splits.

* **Money.** The admin enters the commission as a percent. The amounts are stored:
  ``commission.split_at_pct`` makes them, rounded half up to paise, so commission
  plus payout is always the price. A price-only edit keeps the variant's stored
  ratio (``repriced_split``), as SVC-480's single-variant sync does.
* **"Current %"** is ``rounded_commission_pct``: the exact ratio rounded half up to
  2 dp, the value the response shows. Most catalogue variants have whole-rupee
  amounts (₹240 on ₹799 = 30.0375%, shown 30.04), so a sent percent equal to it is
  no change of percent and the stored ratio stays. Only a different value
  re-splits.
* **Raising the commission** needs ``confirm: true``, after 14 days' notice to
  pros (Vipul 2026-11-20: the same in every city). Decreases, an unchanged
  percent, price-only edits and creates never need it.
* **Customer GST** (``gst_percent``) is read-only: a new variant copies the
  service's first variant's, and sending it is refused.
* **Nothing is deleted** (SP prices and bookings reference variants). Switching
  off the last active variant is refused: the admin switches off the service.
* **The service's base_price** follows its cheapest active variant after every
  write, in the same transaction and audit row (``service_base_price``). It is
  the price the related-services rails show.
* **Nothing else moves.** SP prices (``sp_service_pricing``) and existing
  bookings (``booking_items``) are never touched. The recommended range reads the
  variant's price fresh per request; the from-prices cache catches up in 10 min.

Locks: the service ``FOR UPDATE``, then the variant, the same order as
``update_service`` → ``sync_single_variant_price``. Every variant write takes the
service lock first, so the last-active count and the 2+-variant price lock can't
race another variant write.

Check order on a write: the field rules (422 ``CATALOG_INVALID``), then the label
(409 ``VARIANT_LABEL_TAKEN``), then the notice (409
``COMMISSION_INCREASE_NEEDS_NOTICE``).
"""

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import call_after_commit
from app.exceptions import AppException, ServiceNotFound, ServiceVariantNotFound
from app.models.category import Category
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.models.user import User
from app.schemas.admin_catalog import (
    AdminServiceDetail,
    AdminVariantCreate,
    AdminVariantPriceImpact,
    AdminVariantUpdate,
)
from app.services.admin_audit import AUDIT_FIELDS, AuditActor, audit_admin_action, snapshot
from app.services.admin_catalog_service import (
    DURATION_RANGE,
    SORT_ORDER_RANGE,
    AdminCatalogService,
    CatalogInvalid,
    _base_price,
    _in_range,
)
from app.services.commission import rounded_commission_pct, split_at_pct
from app.services.price_range import is_outside_range, recommended_range
from app.services.standard_variant import repriced_split
from app.utils.money import to_decimal

VARIANT_LABEL_LENGTH = (1, 200)
PCT_LIMIT = Decimal(100)
NOTICE_DAYS = 14
_VARIANT_FIELDS = AUDIT_FIELDS["service_variant"]


class VariantLabelTaken(AppException):
    def __init__(self, label: str):
        super().__init__(
            f"This service already has a variant named {label}",
            status_code=status.HTTP_409_CONFLICT,
            code="VARIANT_LABEL_TAKEN",
            details={"field": "label"},
        )


class CommissionIncreaseNeedsNotice(AppException):
    def __init__(self, old_pct: Decimal, new_pct: Decimal):
        super().__init__(
            "Give pros at least 14 days' notice before saving",
            status_code=status.HTTP_409_CONFLICT,
            code="COMMISSION_INCREASE_NEEDS_NOTICE",
            details={
                "old_pct": float(old_pct),
                "new_pct": float(new_pct),
                "notice_days": NOTICE_DAYS,
            },
        )


class LastActiveVariant(AppException):
    def __init__(self):
        super().__init__(
            "This is the service's last active variant; switch off the service instead",
            status_code=status.HTTP_409_CONFLICT,
            code="LAST_ACTIVE_VARIANT",
        )


# -- value rules ----------------------------------------------------------------


def _label(raw: str) -> str:
    label = raw.strip()
    low, high = VARIANT_LABEL_LENGTH
    if not low <= len(label) <= high:
        raise CatalogInvalid("label", f"Label must be {low} to {high} characters")
    return label


def _commission_pct(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise CatalogInvalid("commission_pct", "commission_pct must be a number")
    if value != value.quantize(Decimal("0.01")):
        raise CatalogInvalid("commission_pct", "commission_pct takes at most 2 decimal places")
    if not Decimal(0) <= value < PCT_LIMIT:
        raise CatalogInvalid("commission_pct", "commission_pct must be at least 0 and below 100")
    return value.quantize(Decimal("0.01"))


def _refuse_gst(body: BaseModel) -> None:
    if "gst_percent" in body.model_fields_set:
        raise CatalogInvalid("gst_percent", "Customer GST is read-only")


def _parse_price(raw: str) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise CatalogInvalid("base_price", "base_price must be a number") from None
    return _base_price(value)


def _fields(variant: ServiceVariant) -> dict:
    return {f: getattr(variant, f) for f in _VARIANT_FIELDS}


# -- the service ----------------------------------------------------------------


class AdminVariantService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.catalog = AdminCatalogService(db)

    async def create(
        self, actor: AuditActor, service_id: uuid.UUID, body: AdminVariantCreate
    ) -> AdminServiceDetail:
        _refuse_gst(body)
        label = _label(body.label)
        price = _base_price(body.base_price)
        pct = _commission_pct(body.commission_pct)
        duration = (
            _in_range("duration_minutes", body.duration_minutes, DURATION_RANGE)
            if body.duration_minutes is not None
            else None
        )
        sort_order = (
            _in_range("sort_order", body.sort_order, SORT_ORDER_RANGE)
            if body.sort_order is not None
            else None
        )

        service = await self.catalog._lock_service(service_id)
        variants = await self._variants(service.id)
        self._check_label_free(variants, label, exclude=None)
        if sort_order is None:
            last = max((v.sort_order for v in variants), default=-1)
            sort_order = min(last + 1, SORT_ORDER_RANGE[1])
        # Customer GST is read-only: the service's first variant's, by sort order.
        gst = variants[0].gst_percent if variants else Decimal("0")

        commission, payout = split_at_pct(price, pct)
        variant = ServiceVariant(
            id=uuid.uuid4(),
            service_id=service.id,
            label=label,
            subcategory=None,
            base_price=price,
            platform_commission=commission,
            provider_payout=payout,
            gst_percent=gst,
            duration_minutes=duration,
            sort_order=sort_order,
            is_active=True,
        )
        await self._flush_variant(variant, add=True)
        after = snapshot("service_variant", variant)
        before = None
        followed = await self._follow_cheapest(service)
        if followed is not None:
            before = {"service_base_price": followed[0]}
            after["service_base_price"] = followed[1]
        await audit_admin_action(
            self.db,
            actor,
            action="variant.create",
            target_type="service_variant",
            target_id=variant.id,
            details={"service_id": str(service.id), "commission_pct": pct},
            before=before,
            after=after,
        )
        # SVC-487: ask the service's SPs to price it, once the save commits. A
        # switched-off service isn't on their pricing screen, so nothing then.
        if service.is_active:
            new_variant_id = str(variant.id)

            def _push() -> None:
                from app.tasks.notification_tasks import send_variant_added_notification

                send_variant_added_notification.delay(variant_id=new_variant_id)

            call_after_commit(self.db, _push)
        return await self.catalog.service_detail(service.id)

    async def update(
        self,
        actor: AuditActor,
        service_id: uuid.UUID,
        variant_id: uuid.UUID,
        body: AdminVariantUpdate,
    ) -> AdminServiceDetail:
        _refuse_gst(body)
        sent = {f: getattr(body, f) for f in body.model_fields_set if f != "confirm"}
        if not sent:
            raise CatalogInvalid("body", "Send at least one field to change")
        for field, value in sent.items():
            if value is None:
                raise CatalogInvalid(field, f"{field} can't be empty")
        label = _label(sent["label"]) if "label" in sent else None
        price = _base_price(sent["base_price"]) if "base_price" in sent else None
        pct = _commission_pct(sent["commission_pct"]) if "commission_pct" in sent else None
        duration = (
            _in_range("duration_minutes", sent["duration_minutes"], DURATION_RANGE)
            if "duration_minutes" in sent
            else None
        )
        sort_order = (
            _in_range("sort_order", sent["sort_order"], SORT_ORDER_RANGE)
            if "sort_order" in sent
            else None
        )

        service = await self.catalog._lock_service(service_id)
        variant = await self._lock_variant(service.id, variant_id)
        old = _fields(variant)

        if label is not None and label != variant.label:
            self._check_label_free(await self._variants(service.id), label, exclude=variant.id)

        old_pct = rounded_commission_pct(variant)
        # The same percent as shown is no change: the stored ratio stays.
        pct_changed = pct is not None and pct != old_pct
        if pct_changed and pct > old_pct and not body.confirm:
            raise CommissionIncreaseNeedsNotice(old_pct, pct)

        new_price = price if price is not None else to_decimal(variant.base_price)
        if pct_changed:
            commission, payout = split_at_pct(new_price, pct)
        elif new_price != to_decimal(variant.base_price):
            commission, payout = repriced_split(variant, new_price)
        else:
            commission, payout = variant.platform_commission, variant.provider_payout

        variant.base_price = new_price
        variant.platform_commission = commission
        variant.provider_payout = payout
        if label is not None:
            variant.label = label
        if duration is not None:
            variant.duration_minutes = duration
        if sort_order is not None:
            variant.sort_order = sort_order

        new = _fields(variant)
        changed = [f for f in _VARIANT_FIELDS if new[f] != old[f]]
        if not changed:
            return await self.catalog.service_detail(service.id)

        await self._flush_variant(variant, add=False)
        before = {f: old[f] for f in changed}
        after = {f: new[f] for f in changed}
        followed = await self._follow_cheapest(service)
        if followed is not None:
            before["service_base_price"], after["service_base_price"] = followed
        details = {"service_id": str(service.id)}
        if pct_changed:
            details.update(old_pct=old_pct, new_pct=pct, confirm=body.confirm)
        await audit_admin_action(
            self.db,
            actor,
            action="variant.update",
            target_type="service_variant",
            target_id=variant.id,
            details=details,
            before=before,
            after=after,
        )
        return await self.catalog.service_detail(service.id)

    async def set_active(
        self, actor: AuditActor, service_id: uuid.UUID, variant_id: uuid.UUID, active: bool
    ) -> AdminServiceDetail:
        """Off: refused for the last active variant. SP prices on it stay as they
        are, and come back when it's switched on; in-flight bookings carry on.
        Already in that state: 200, no audit row."""
        service = await self.catalog._lock_service(service_id)
        variant = await self._lock_variant(service.id, variant_id)
        if variant.is_active != active:
            if not active:
                others = await self.db.scalar(
                    select(func.count()).where(
                        ServiceVariant.service_id == service.id,
                        ServiceVariant.is_active.is_(True),
                        ServiceVariant.id != variant.id,
                    )
                )
                if not others:
                    raise LastActiveVariant()
            variant.is_active = active
            await self.db.flush()
            before, after = {"is_active": not active}, {"is_active": active}
            followed = await self._follow_cheapest(service)
            if followed is not None:
                before["service_base_price"], after["service_base_price"] = followed
            await audit_admin_action(
                self.db,
                actor,
                action="variant.activate" if active else "variant.deactivate",
                target_type="service_variant",
                target_id=variant.id,
                details={"service_id": str(service.id)},
                before=before,
                after=after,
            )
        return await self.catalog.service_detail(service.id)

    async def price_impact(
        self, service_id: uuid.UUID, variant_id: uuid.UUID, raw_price: str
    ) -> AdminVariantPriceImpact:
        """Read-only, no audit. The population is the ranking's and SVC-413's
        (an active price on this variant, KYC verified, active account), in
        every area."""
        price = _parse_price(raw_price)
        row = (
            await self.db.execute(
                select(
                    ServiceVariant.base_price,
                    Category.id,
                    Category.is_sensitive,
                    Category.bounds_pct,
                )
                .join(Service, Service.id == ServiceVariant.service_id)
                .join(Category, Category.id == Service.category_id)
                .where(ServiceVariant.id == variant_id, ServiceVariant.service_id == service_id)
            )
        ).first()
        if row is None:
            if await self.db.get(Service, service_id) is None:
                raise ServiceNotFound(service_id)
            raise ServiceVariantNotFound(service_id, variant_id)
        current, category_id, is_sensitive, bounds_pct = row
        range_now = recommended_range(
            to_decimal(current), is_sensitive, bounds_pct, category_id=category_id
        )
        range_after = recommended_range(price, is_sensitive, bounds_pct, category_id=category_id)

        prices = (
            await self.db.scalars(
                select(SpServicePricing.price_amount)
                .join(User, User.id == SpServicePricing.sp_id)
                .join(ProviderProfile, ProviderProfile.user_id == SpServicePricing.sp_id)
                .where(
                    SpServicePricing.service_variant_id == variant_id,
                    SpServicePricing.is_active.is_(True),
                    User.is_active.is_(True),
                    ProviderProfile.kyc_status == KYCStatus.VERIFIED,
                )
            )
        ).all()
        outside_now = outside_after = moving_out = moving_in = 0
        for sp_price in prices:
            was_out = is_outside_range(sp_price, range_now)
            is_out = is_outside_range(sp_price, range_after)
            outside_now += was_out
            outside_after += is_out
            moving_out += is_out and not was_out
            moving_in += was_out and not is_out
        return AdminVariantPriceImpact(
            sp_prices=len(prices),
            outside_now=outside_now,
            outside_after=outside_after,
            moving_out=moving_out,
            moving_in=moving_in,
        )

    # -- helpers ----------------------------------------------------------------

    async def _variants(self, service_id: uuid.UUID) -> list[ServiceVariant]:
        """Every variant of the service, active or not, by (sort_order, id). The
        caller holds the service lock."""
        return list(
            (
                await self.db.scalars(
                    select(ServiceVariant)
                    .where(ServiceVariant.service_id == service_id)
                    .order_by(ServiceVariant.sort_order, ServiceVariant.id)
                )
            ).all()
        )

    async def _lock_variant(self, service_id: uuid.UUID, variant_id: uuid.UUID) -> ServiceVariant:
        variant = await self.db.scalar(
            select(ServiceVariant)
            .where(ServiceVariant.id == variant_id, ServiceVariant.service_id == service_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if variant is None:
            raise ServiceVariantNotFound(service_id, variant_id)
        return variant

    @staticmethod
    def _check_label_free(
        variants: list[ServiceVariant], label: str, *, exclude: uuid.UUID | None
    ) -> None:
        """Unique per service in any case. The DB's own unique is case-sensitive."""
        for other in variants:
            if other.id != exclude and other.label.lower() == label.lower():
                raise VariantLabelTaken(other.label)

    async def _flush_variant(self, variant: ServiceVariant, *, add: bool) -> None:
        """A savepoint, so a racing save of the exact same label is a 409 and the
        rest of the transaction survives. Known gap, accepted (SVC-481, no
        migration): ``uix_variant_service_label`` is case-sensitive, so two saves
        racing on "1 BHK" and "1 bhk" can both get in. Outside a race the
        any-case check in ``_check_label_free`` refuses it."""
        try:
            async with self.db.begin_nested():
                if add:
                    self.db.add(variant)
                await self.db.flush()
        except IntegrityError:
            raise VariantLabelTaken(variant.label) from None

    async def _follow_cheapest(self, service: Service) -> tuple[Decimal, Decimal] | None:
        """Set the service's base_price to its cheapest active variant's. Returns
        ``(before, after)`` when that moved it, else None. No active variant
        (only reachable by editing a switched-off one on a service with none on):
        left as it is."""
        await self.db.flush()
        cheapest = await self.db.scalar(
            select(func.min(ServiceVariant.base_price)).where(
                ServiceVariant.service_id == service.id, ServiceVariant.is_active.is_(True)
            )
        )
        old = to_decimal(service.base_price)
        if cheapest is None or to_decimal(cheapest) == old:
            return None
        service.base_price = to_decimal(cheapest)
        await self.db.flush()
        return old, service.base_price
