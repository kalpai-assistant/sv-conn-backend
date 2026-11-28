"""Find catalog services, and other SPs' pending requests, similar to a proposal (SVC-437).

Callers depend on ``CatalogSimilarityDetector`` only. Today's implementation is
pg_trgm (``TrgmSimilarityDetector`` over ``services``,
``PendingRequestOverlapDetector`` over PENDING ``service_requests``, merged by
``CompositeSimilarityDetector``). Swapping to embeddings later means a new
class that implements the Protocol, plus a one-line change in
``app.routers.catalog.get_similarity_detector``. No caller changes.

Known pg_trgm limit: a shared word drives the score. "Deep cleaning" vs
"AC cleaning" scores about 0.53 and is returned, even though they are
different services. Stripping generic words broke other cases on the real
catalog ("AC repair" vs "AC installation" scored 1.00), so the ticket's
thresholds stay as written and the test suite pins the limit for the
embeddings implementation to flip.

The ``similarity() > x`` predicates below can't use the GIN trigram indexes
on ``services`` (only the ``%`` operator can). At beta's catalog size a
sequential scan is well within budget. The indexes are there for a future
``%`` / ``word_similarity`` query.
"""

import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.service import Service
from app.models.service_request import ServiceRequest, ServiceRequestStatus

NAME_THRESHOLD = 0.3
DESCRIPTION_THRESHOLD = 0.4
# Names are the canonical identifier; descriptions vary wildly in length.
DESCRIPTION_WEIGHT = 0.6
# Below 3 characters a name has no trigram worth matching. The SP app calls
# this while the user types, so that is an empty result, not an error.
MIN_NAME_LENGTH = 3

Source = Literal["catalog", "pending_request"]


@dataclass(frozen=True)
class SimilarityHit:
    """One similar service or pending request.

    A catalog hit has ``service_id``; a pending hit has ``request_id``. A
    pending hit deliberately has no field for the other SP's id, description
    or price: only its proposed name, category and score are shown.
    """

    service_id: uuid.UUID | None
    request_id: uuid.UUID | None
    service_name: str
    category_name: str
    score: float  # 0.0 to 1.0, higher = more similar
    source: Source


class CatalogSimilarityDetector(Protocol):
    async def find_similar(
        self,
        proposed_name: str,
        proposed_description: str = "",
        category_id: uuid.UUID | None = None,
        limit: int = 5,
        requesting_sp_id: uuid.UUID | None = None,
    ) -> list[SimilarityHit]: ...


def _in_category_tree(column, category_id: uuid.UUID) -> ColumnElement[bool]:
    """``column`` is the category or one of its children.

    Categories are two levels, so one level of children covers an L1 tile;
    an L2 has no children and matches only itself.
    """
    children = select(Category.id).where(Category.parent_id == category_id)
    return or_(column == category_id, column.in_(children))


def _score(name_col, description_col, proposed_name: str, proposed_description: str):
    name_sim = func.similarity(name_col, proposed_name)
    description_sim = func.similarity(description_col, proposed_description)
    # GREATEST skips NULL, so a service without a description scores on its name.
    score = func.greatest(name_sim, description_sim * DESCRIPTION_WEIGHT)
    matches = or_(name_sim > NAME_THRESHOLD, description_sim > DESCRIPTION_THRESHOLD)
    return score, matches


class TrgmSimilarityDetector:
    """pg_trgm over active catalog services."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_similar(
        self,
        proposed_name: str,
        proposed_description: str = "",
        category_id: uuid.UUID | None = None,
        limit: int = 5,
        requesting_sp_id: uuid.UUID | None = None,
    ) -> list[SimilarityHit]:
        score, matches = _score(
            Service.name, Service.description, proposed_name, proposed_description
        )
        stmt = (
            select(Service.id, Service.name, Category.name, score.label("score"))
            .join(Category, Category.id == Service.category_id)
            .where(Service.is_active.is_(True), matches)
            .order_by(score.desc(), Service.id)
            .limit(limit)
        )
        if category_id is not None:
            stmt = stmt.where(_in_category_tree(Service.category_id, category_id))
        rows = (await self.db.execute(stmt)).all()
        return [
            SimilarityHit(
                service_id=service_id,
                request_id=None,
                service_name=name,
                category_name=category_name,
                score=float(row_score),
                source="catalog",
            )
            for service_id, name, category_name, row_score in rows
        ]


class PendingRequestOverlapDetector:
    """pg_trgm over other SPs' PENDING service requests.

    ``requesting_sp_id`` excludes that SP's own requests; ``None`` (an admin
    caller) includes every PENDING request.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_similar(
        self,
        proposed_name: str,
        proposed_description: str = "",
        category_id: uuid.UUID | None = None,
        limit: int = 5,
        requesting_sp_id: uuid.UUID | None = None,
    ) -> list[SimilarityHit]:
        score, matches = _score(
            ServiceRequest.proposed_name,
            ServiceRequest.proposed_description,
            proposed_name,
            proposed_description,
        )
        stmt = (
            select(
                ServiceRequest.id,
                ServiceRequest.proposed_name,
                Category.name,
                score.label("score"),
            )
            .join(Category, Category.id == ServiceRequest.proposed_category_id)
            .where(ServiceRequest.status == ServiceRequestStatus.PENDING.value, matches)
            .order_by(score.desc(), ServiceRequest.id)
            .limit(limit)
        )
        if requesting_sp_id is not None:
            stmt = stmt.where(ServiceRequest.sp_id != requesting_sp_id)
        if category_id is not None:
            stmt = stmt.where(_in_category_tree(ServiceRequest.proposed_category_id, category_id))
        rows = (await self.db.execute(stmt)).all()
        return [
            SimilarityHit(
                service_id=None,
                request_id=request_id,
                service_name=name,
                category_name=category_name,
                score=float(row_score),
                source="pending_request",
            )
            for request_id, name, category_name, row_score in rows
        ]


class CompositeSimilarityDetector:
    """Runs every detector and returns the top ``limit`` hits overall, by score.

    Each detector is asked for ``limit`` too, which is always enough to fill the
    overall top ``limit``. They run one after another because one AsyncSession
    can't run two queries at once.
    """

    def __init__(self, detectors: list[CatalogSimilarityDetector]):
        self.detectors = detectors

    async def find_similar(
        self,
        proposed_name: str,
        proposed_description: str = "",
        category_id: uuid.UUID | None = None,
        limit: int = 5,
        requesting_sp_id: uuid.UUID | None = None,
    ) -> list[SimilarityHit]:
        proposed_name = proposed_name.strip()
        proposed_description = proposed_description.strip()
        if len(proposed_name) < MIN_NAME_LENGTH or limit < 1:
            return []
        hits: list[SimilarityHit] = []
        for detector in self.detectors:
            hits.extend(
                await detector.find_similar(
                    proposed_name,
                    proposed_description,
                    category_id=category_id,
                    limit=limit,
                    requesting_sp_id=requesting_sp_id,
                )
            )
        # Ties: catalog before pending (an existing service is the better
        # answer), then by name so the order is stable.
        hits.sort(key=lambda h: (-h.score, h.source != "catalog", h.service_name))
        return hits[:limit]
