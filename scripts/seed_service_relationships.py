"""Auto-seed `service_relationships` from category proximity (SVC-291).

Idempotent — a re-run against the same catalogue writes zero new rows.
Hand-curated overrides survive because the seed skips inserts that would
violate `uix_service_relationship` on (source, related, type).

For every active service in the catalogue:

    upsell:   top N=5 OTHER services in the SAME sub-category as source.
              Ordered by base_price DESC then name ASC — the biggest-ticket
              same-context add-on lands first.
    related:  the next N=10 same-sub-category services, picked up where
              `upsell` left off. Same ordering; think of `upsell` + `related`
              as one ranked list of 15 same-sub-cat entries split by row 6.
    hook:     top N=3 services from OTHER sub-categories under the SAME L1.
              Ordered by base_price DESC then name ASC — highest-value
              cross-domain nudge first.

Determinism: every ordering has a secondary `name ASC` tiebreaker so a
re-run picks the same rows in the same order even when several rows have
identical `base_price` (which the catalogue does — many launch-summary
services share round-number prices). Same shape as the seed_categories
script's idempotency.

L1 detection: a category with `parent_id IS NULL` is L1. A service whose
category is itself L1 (Miscellaneous case) has no L1 hooks by definition —
there is no "other sub-category under the same L1" for it. That service
gets `upsell` + `related` from its own L1 siblings, no `hook` rows.

Run:
    uv run python scripts/seed_service_relationships.py
    uv run python scripts/seed_service_relationships.py --dry-run

The dry-run prints per-source counts + the totals without touching the DB —
use it against prod before the real run to see what the row count will be.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.category import Category
from app.models.service import Service
from app.models.service_relationship import RelationshipType, ServiceRelationship

# Defaults straight from the ticket — bump / override via CLI flags if a
# future seed re-run wants different counts. Keeping them as module-level
# constants makes it obvious what the algorithm is committing to.
DEFAULT_UPSELL = 5
DEFAULT_RELATED = 10
DEFAULT_HOOK = 3


async def _fetch_catalogue(db):
    """Load every active service + a `category_id → parent_id` map.

    Returned in a shape that lets the ranking logic run in-memory without
    any per-service SQL: 363 services + a couple dozen categories is small
    enough to sort in Python far faster than another round trip.
    """
    services = (
        (await db.execute(select(Service).where(Service.is_active.is_(True)))).scalars().all()
    )
    categories = (await db.execute(select(Category))).scalars().all()
    parent_of = {c.id: c.parent_id for c in categories}
    return list(services), parent_of


def _rank(services: list[Service]) -> list[Service]:
    """Deterministic seed ordering: base_price DESC then name ASC.

    Both keys included on every call so a re-run against unchanged data
    produces byte-identical rows in byte-identical order — that's what
    makes the seed idempotent-in-content, not just idempotent-in-existence.
    """
    return sorted(services, key=lambda s: (-float(s.base_price), s.name))


def _pick_edges(
    source: Service,
    services: list[Service],
    parent_of: dict[UUID, UUID | None],
    n_upsell: int,
    n_related: int,
    n_hook: int,
) -> list[tuple[UUID, str, int]]:
    """Return `(related_service_id, relationship_type, sort_order)` for one source.

    Broken out so the CLI dry-run can compute counts without touching the DB.
    """
    edges: list[tuple[UUID, str, int]] = []
    source_l2_id = source.category_id
    source_l1_id = parent_of.get(source_l2_id)  # None if the source is under an L1 itself

    same_l2 = _rank([s for s in services if s.id != source.id and s.category_id == source_l2_id])
    edges.extend(
        (s.id, RelationshipType.UPSELL.value, idx) for idx, s in enumerate(same_l2[:n_upsell])
    )
    edges.extend(
        (s.id, RelationshipType.RELATED.value, idx)
        for idx, s in enumerate(same_l2[n_upsell : n_upsell + n_related])
    )

    # `hook` needs an L1 parent to reason about "other sub-categories under
    # the same L1". A service whose category is itself L1 (Miscellaneous)
    # has no such peers — skip cleanly rather than inventing weird cross-L1
    # picks the ticket explicitly excludes.
    if source_l1_id is not None:
        cross_l2 = _rank(
            [
                s
                for s in services
                if s.id != source.id
                and s.category_id != source_l2_id
                and parent_of.get(s.category_id) == source_l1_id
            ]
        )
        edges.extend(
            (s.id, RelationshipType.HOOK.value, idx) for idx, s in enumerate(cross_l2[:n_hook])
        )

    return edges


async def _upsert(db, source_id: UUID, edges: list[tuple[UUID, str, int]]) -> int:
    """Insert edges, skipping any that would collide with an existing row.

    Uses Postgres `INSERT ... ON CONFLICT DO NOTHING` on the unique index so
    a re-run touches zero existing rows — hand-curated `sort_order` bumps or
    added edges survive. Returns the number of rows actually written.
    """
    if not edges:
        return 0
    payload = [
        {
            "source_service_id": source_id,
            "related_service_id": rel_id,
            "relationship_type": rel_type,
            "sort_order": sort_order,
        }
        for rel_id, rel_type, sort_order in edges
    ]
    stmt = (
        pg_insert(ServiceRelationship)
        .values(payload)
        .on_conflict_do_nothing(constraint="uix_service_relationship")
    )
    result = await db.execute(stmt)
    return result.rowcount or 0


async def seed(
    *,
    dry_run: bool,
    n_upsell: int,
    n_related: int,
    n_hook: int,
) -> None:
    async with AsyncSessionLocal() as db:
        services, parent_of = await _fetch_catalogue(db)
        print(f"Loaded {len(services)} active services across {len(parent_of)} categories")

        total_inserted = 0
        per_type: dict[str, int] = defaultdict(int)
        for source in services:
            edges = _pick_edges(source, services, parent_of, n_upsell, n_related, n_hook)
            for _, rel_type, _ in edges:
                per_type[rel_type] += 1
            if dry_run:
                continue
            inserted = await _upsert(db, source.id, edges)
            total_inserted += inserted

        if dry_run:
            print("Dry-run — no rows written.")
            print(f"  Would attempt {sum(per_type.values())} edges total:")
        else:
            await db.commit()
            print(f"Inserted {total_inserted} new rows (existing rows preserved).")
        for rel_type, count in sorted(per_type.items()):
            print(f"    {rel_type:8s} {count}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute counts without touching the DB. Safe to run against prod.",
    )
    parser.add_argument(
        "--upsell", type=int, default=DEFAULT_UPSELL, help="N per source (default: 5)"
    )
    parser.add_argument(
        "--related", type=int, default=DEFAULT_RELATED, help="N per source (default: 10)"
    )
    parser.add_argument("--hook", type=int, default=DEFAULT_HOOK, help="N per source (default: 3)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        seed(
            dry_run=args.dry_run,
            n_upsell=args.upsell,
            n_related=args.related,
            n_hook=args.hook,
        )
    )
