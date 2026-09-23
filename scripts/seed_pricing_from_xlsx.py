"""Reseed the catalogue from ``backend/EazyServ_Pune_Pricing_List.xlsx``.

Wipes every row in services/service_variants (and provider_services which
FKs into services), then reloads services + variants under the categories
that ``seed_categories.py`` already created. Categories themselves are
never touched — the SVC-286 L1/L2 hierarchy is a precondition of this
script, not something it manages (SVC-312).

Refuses to run if the SVC-286 hierarchy isn't in place: seed_categories.py
must have run against the current DB first. This prevents the historical
bug (SVC-312) where a re-run wiped the 7 L1 tiles + parent_id links and
left the home page rendering the flat 18-category list.

The workbook has two shape families:

    "Pricing" summary                    — 12 categories, ~76 rows,
                                           customer price + provider payout,
                                           split ~70/30, GST not modelled
                                           (col says "Review GST applicability")

    5 single-price verticals             — RO Purifier / Furniture / Toilet Clean
    (Base + Payout + Commission + GST      Commercial / CCTV & Light Event /
     + Total)                              Nail Art. Split 65/35, GST 18%.

    Vehicle Wash & Cleaning              — same 65/35, 18% GST, but the row
                                           is 4-vehicle-wide (Hatchback / Sedan
                                           / SUV / MUV). Each becomes one
                                           variant, with vehicle type as
                                           the variant label.

Safety net: refuses to run if any ``booking_items`` rows exist unless
``--force`` is passed. beta today has zero bookings so this is a no-op
guard; but the same script running against prod later must not silently
delete a paying customer's booking cart.

Usage (against beta):
    export DATABASE_URL=$(aws ssm get-parameter --name /eazyserv/beta/DATABASE_URL \\
        --with-decryption --query 'Parameter.Value' --output text --profile eazyserv)
    uv run --with openpyxl python scripts/seed_pricing_from_xlsx.py

Usage (against local):
    uv run python scripts/seed_pricing_from_xlsx.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from openpyxl import load_workbook  # noqa: E402
from sqlalchemy import delete, func, select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models.booking_item import BookingItem  # noqa: E402
from app.models.category import Category  # noqa: E402
from app.models.provider_service import ProviderService  # noqa: E402
from app.models.service import Service  # noqa: E402
from app.models.service_variant import ServiceVariant  # noqa: E402
from app.seed_data.l1_hierarchy import L2_SLUGS, MISCELLANEOUS_L1_SLUG  # noqa: E402

DEFAULT_XLSX = Path(__file__).resolve().parent.parent / "EazyServ_Pune_Pricing_List.xlsx"

# Sheet name → display name for the category we create when loading that
# sheet's rows. The workbook has "Furniture " (trailing space), "Toilet
# Clean for Commercial" (verbose), etc — clean these up here so the app
# UI shows something readable.
VERTICAL_CATEGORY_NAMES: dict[str, str] = {
    "RO Purifier": "RO Purifier",
    "Furniture ": "Furniture",
    "Vehicle Wash & Cleaning": "Vehicle Wash & Cleaning",
    "Toilet Clean for Commercial": "Toilet Cleaning (Commercial)",
    "CCTV & Light Event": "CCTV & Light Event",
    "Nail Art": "Nail Art",
}

# Vehicle types on the Vehicle Wash sheet, in the order the price columns
# appear (col idx 3..6 are Hatchback/Sedan/SUV/MUV prices; then 4 groups
# of 4 cols each = payout, platform, gst, total for each vehicle type).
VEHICLE_TYPES = ["Hatchback", "Sedan", "SUV", "MUV"]


def _slug(name: str) -> str:
    """Slugify a category name to fit the categories.slug UNIQUE column."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _num(v) -> Decimal | None:
    """Coerce an xlsx cell value to Decimal or None. Accepts int/float/str."""
    if v is None or v == "":
        return None
    if isinstance(v, int | float):
        return Decimal(str(v))
    try:
        return Decimal(str(v).replace(",", "").strip())
    except Exception:
        return None


@dataclass
class VariantRow:
    """One priced line ready to seed."""

    category_name: str
    service_name: str
    service_description: str | None
    label: str
    subcategory: str | None
    base_price: Decimal
    provider_payout: Decimal
    platform_commission: Decimal
    gst_percent: Decimal
    materials_note: str | None
    sort_order: int


def _parse_pricing_summary(ws) -> list[VariantRow]:
    """The "Pricing" summary sheet.

    Header: Category | Service | Variant / Unit | EazyServ Customer Price (₹)
          | Professional Payout (₹) | EazyServ Gross Margin (₹) | Margin %
          | GST Included? | Notes
    """
    out: list[VariantRow] = []
    sort_index = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        category, service, label, price, payout, margin, _pct, _gst, _notes = row[:9]
        if not category or not service:
            continue
        p_price = _num(price)
        p_payout = _num(payout)
        p_margin = _num(margin)
        if p_price is None or p_payout is None:
            continue
        # Prefer the sheet's explicit margin if present, otherwise derive.
        commission = p_margin if p_margin is not None else (p_price - p_payout)
        out.append(
            VariantRow(
                category_name=str(category).strip(),
                service_name=str(service).strip(),
                service_description=None,
                label=(str(label).strip() if label else "Standard"),
                subcategory=None,
                base_price=p_price,
                provider_payout=p_payout,
                platform_commission=commission,
                gst_percent=Decimal("0"),
                materials_note=None,
                sort_order=sort_index,
            )
        )
        sort_index += 1
    return out


def _parse_single_price_vertical(ws, category_name: str) -> list[VariantRow]:
    """RO Purifier / Furniture / Toilet Clean / CCTV / Nail Art.

    Header: Category (subcategory) | Service | Unit / Scope | Base Price
          | Payout | Commission | GST | Total | Materials | Notes
    """
    out: list[VariantRow] = []
    sort_index = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        subcat, service, label, price, payout, commission, _gst, _total, materials, _notes = row[
            :10
        ]
        if not service:
            continue
        p_price = _num(price)
        p_payout = _num(payout)
        p_comm = _num(commission)
        if p_price is None or p_payout is None:
            continue
        if p_comm is None:
            p_comm = p_price - p_payout
        out.append(
            VariantRow(
                category_name=category_name,
                service_name=str(service).strip(),
                service_description=None,
                label=(str(label).strip() if label else "Standard"),
                subcategory=(str(subcat).strip() if subcat else None),
                base_price=p_price,
                provider_payout=p_payout,
                platform_commission=p_comm,
                # Verticals plan for 18% GST; the column exists on every row.
                gst_percent=Decimal("18"),
                materials_note=(str(materials).strip() if materials else None),
                sort_order=sort_index,
            )
        )
        sort_index += 1
    return out


def _parse_vehicle_wash(ws, category_name: str) -> list[VariantRow]:
    """Vehicle Wash sheet — 23 cols, one row per service × 4 vehicle types.

    Cols 3..6 are prices (Hatchback / Sedan / SUV / MUV).
    Cols 7..22 are 4 groups of (Payout, Platform, GST, Total) per vehicle.
    """
    out: list[VariantRow] = []
    sort_index = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        subcat, service, package_scope = row[0], row[1], row[2]
        if not service:
            continue
        prices = [_num(row[3 + i]) for i in range(4)]
        # Groups start at col 7. Each group is 4 cols wide.
        groups = []
        for gi in range(4):
            base = 7 + gi * 4
            payout = _num(row[base])
            platform = _num(row[base + 1])
            groups.append((payout, platform))
        for i, vt in enumerate(VEHICLE_TYPES):
            price = prices[i]
            payout, platform = groups[i]
            if price is None or payout is None:
                continue
            if platform is None:
                platform = price - payout
            out.append(
                VariantRow(
                    category_name=category_name,
                    service_name=str(service).strip(),
                    service_description=(str(package_scope).strip() if package_scope else None),
                    label=vt,
                    subcategory=(str(subcat).strip() if subcat else None),
                    base_price=price,
                    provider_payout=payout,
                    platform_commission=platform,
                    gst_percent=Decimal("18"),
                    materials_note=None,
                    sort_order=sort_index * 10 + i,
                )
            )
        sort_index += 1
    return out


def parse_workbook(path: Path) -> list[VariantRow]:
    wb = load_workbook(str(path), data_only=True)
    rows: list[VariantRow] = []
    for name in wb.sheetnames:
        ws = wb[name]
        if name == "Pricing":
            rows.extend(_parse_pricing_summary(ws))
        elif name == "Vehicle Wash & Cleaning":
            rows.extend(_parse_vehicle_wash(ws, VERTICAL_CATEGORY_NAMES[name]))
        elif name in VERTICAL_CATEGORY_NAMES:
            rows.extend(_parse_single_price_vertical(ws, VERTICAL_CATEGORY_NAMES[name]))
        else:
            print(f"! Unknown sheet: {name!r} — skipping")
    return rows


async def ensure_hierarchy_seeded(db) -> dict[str, Category]:
    """Precondition check for SVC-312: seed_categories.py must have run.

    Loads every category currently in the DB and confirms:

    * All 17 SVC-286 L2 slugs are present.
    * The ``miscellaneous`` L1 slug is present.

    If anything is missing we bail out BEFORE the wipe. Rerunning the pricing
    seed against a half-seeded DB used to wipe the L1 hierarchy on the way
    in and leave the home page rendering the flat 18-category list; this
    guard prevents a repeat.

    Returns a slug → Category dict so the caller can look up FK targets
    without re-querying.
    """
    all_cats = (await db.execute(select(Category))).scalars().all()
    by_slug = {c.slug: c for c in all_cats}

    required = L2_SLUGS | {MISCELLANEOUS_L1_SLUG}
    missing = sorted(required - by_slug.keys())
    if missing:
        raise SystemExit(
            "Refusing to reseed pricing: the SVC-286 category hierarchy isn't "
            "in place yet. Missing slugs: "
            f"{', '.join(missing)}. Run `uv run python scripts/seed_categories.py` "
            "first (see backend/CLAUDE.md → Seed ritual, SVC-312)."
        )
    return by_slug


async def wipe_services_and_variants(db, force: bool = False) -> None:
    """DELETE all rows from services/variants (and provider_services which
    FKs into services). Categories are deliberately preserved — the
    SVC-286 hierarchy is a precondition of this script, not something it
    manages (SVC-312).

    Order matters: variants → services (FKs point the other way).
    provider_services also references services, so it goes first.
    """
    bi_count = (await db.execute(select(func.count()).select_from(BookingItem))).scalar_one()
    if bi_count and not force:
        raise SystemExit(
            f"Refusing to wipe catalog: {bi_count} booking_items rows exist. "
            "Pass --force to proceed anyway (this will fail on FK)."
        )
    await db.execute(delete(ProviderService))
    await db.execute(delete(ServiceVariant))
    await db.execute(delete(Service))
    await db.flush()


async def seed(rows: list[VariantRow], force: bool = False) -> None:
    """Insert every parsed row, upserting the (category, service) uniqueness.

    Categories are looked up by slug against the existing hierarchy
    (seeded by ``scripts/seed_categories.py``) — this script neither
    creates nor deletes categories (SVC-312).
    """
    if not rows:
        raise SystemExit("Parsed zero rows from workbook — refusing to wipe an empty catalog.")

    async with AsyncSessionLocal() as db:
        cats_by_slug = await ensure_hierarchy_seeded(db)
        await wipe_services_and_variants(db, force=force)

        # Map each workbook category name to its existing L2 (or Miscellaneous
        # L1) row by slug. If a workbook row references a slug the DB doesn't
        # know about — a new sheet name, or a typo — refuse loudly rather
        # than silently dropping the pricing rows.
        categories_by_name: dict[str, Category] = {}
        unknown_slugs: set[str] = set()
        for r in rows:
            if r.category_name in categories_by_name:
                continue
            slug = _slug(r.category_name)
            cat = cats_by_slug.get(slug)
            if cat is None:
                unknown_slugs.add(slug)
                continue
            categories_by_name[r.category_name] = cat
        if unknown_slugs:
            raise SystemExit(
                "Workbook references categories not present in the DB: "
                f"{', '.join(sorted(unknown_slugs))}. Either add them to "
                "app.seed_data.l1_hierarchy + seed_categories.py or fix the "
                "workbook sheet names."
            )

        # Materialise services keyed by (category_id, name). The workbook
        # can name the same service under two categories (Bathroom deep
        # cleaning appears in "Cleaning" and could plausibly appear in a
        # commercial cleaning vertical) — those are two different services
        # semantically, so they get two rows.
        services_by_key: dict[tuple, Service] = {}
        for r in rows:
            cat = categories_by_name[r.category_name]
            key = (cat.id, r.service_name)
            if key not in services_by_key:
                svc = Service(
                    category_id=cat.id,
                    name=r.service_name,
                    description=r.service_description,
                    # Set to the variant min after all variants are seeded
                    # (see below). base_price stays NOT NULL for the current
                    # mobile app's "starting at" display.
                    base_price=r.base_price,
                    duration_minutes=60,
                    is_active=True,
                )
                services_by_key[key] = svc
                db.add(svc)
        await db.flush()

        # Variants. Group by (cat, service) so we can (a) compute the min
        # for services.base_price and (b) assign a per-service sort_order
        # that starts at 0 (the sheet's global sort_order is preserved as
        # relative ordering within each service).
        per_service: dict[tuple, list[VariantRow]] = {}
        for r in rows:
            cat = categories_by_name[r.category_name]
            per_service.setdefault((cat.id, r.service_name), []).append(r)

        variant_count = 0
        for (cat_id, svc_name), group in per_service.items():
            svc = services_by_key[(cat_id, svc_name)]
            # Dedup by label within a service — if the sheet has two rows
            # with the same label for the same service, the DB unique
            # constraint would reject the second. Warn + skip rather than
            # crash mid-transaction.
            seen_labels: set[str] = set()
            for i, r in enumerate(group):
                if r.label in seen_labels:
                    print(f"! Duplicate variant label {r.label!r} for {svc_name!r} — skipping")
                    continue
                seen_labels.add(r.label)
                db.add(
                    ServiceVariant(
                        service_id=svc.id,
                        label=r.label,
                        subcategory=r.subcategory,
                        base_price=r.base_price,
                        provider_payout=r.provider_payout,
                        platform_commission=r.platform_commission,
                        gst_percent=r.gst_percent,
                        materials_note=r.materials_note,
                        sort_order=i,
                        is_active=True,
                    )
                )
                variant_count += 1
            # services.base_price = min of its variants' base_price ("starting at").
            svc.base_price = min(r.base_price for r in group)

        await db.commit()

    print(
        f"Seeded {len(categories_by_name)} categories, "
        f"{len(services_by_key)} services, {variant_count} variants."
    )


def main():
    ap = argparse.ArgumentParser(description="Reseed pricing catalog from xlsx.")
    ap.add_argument(
        "--xlsx",
        type=Path,
        default=DEFAULT_XLSX,
        help=f"Path to the pricing workbook (default: {DEFAULT_XLSX.name})",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Wipe even if booking_items rows exist (will fail on FK).",
    )
    args = ap.parse_args()
    if not args.xlsx.exists():
        raise SystemExit(f"Workbook not found: {args.xlsx}")
    rows = parse_workbook(args.xlsx)
    print(f"Parsed {len(rows)} priced rows from {args.xlsx.name}")
    asyncio.run(seed(rows, force=args.force))


if __name__ == "__main__":
    main()
