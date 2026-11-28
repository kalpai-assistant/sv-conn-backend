"""SP-facing catalog tools (SVC-437).

``POST /catalog/similarity`` is SP-only for now. The admin "similar pending
request" flag (a later story) calls the detector directly, with
``requesting_sp_id=None``.
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.user import User
from app.schemas.catalog_similarity import SimilarityHitResponse, SimilarityRequest
from app.services.catalog_similarity import (
    CatalogSimilarityDetector,
    CompositeSimilarityDetector,
    PendingRequestOverlapDetector,
    TrgmSimilarityDetector,
)
from app.utils.rate_limiter import rate_limit

router = APIRouter(tags=["Catalog"])

# 30 / SP / minute: inline suggestions are debounced, so this is generous.
similarity_rate_limit = rate_limit("catalog_similarity", max_requests=30, window_seconds=60)


def get_similarity_detector(db: AsyncSession = Depends(get_db)) -> CatalogSimilarityDetector:
    """The one place the implementation is chosen. Swapping to embeddings
    changes this line, not the callers."""
    return CompositeSimilarityDetector(
        [TrgmSimilarityDetector(db), PendingRequestOverlapDetector(db)]
    )


@router.post(
    "/catalog/similarity",
    response_model=list[SimilarityHitResponse],
    # Order matters: route dependencies resolve first and in this order, so the
    # role check refuses a non-SP before the rate limit counts the call.
    dependencies=[Depends(require_role("provider")), Depends(similarity_rate_limit)],
)
async def find_similar_services(
    body: SimilarityRequest,
    # Cached per request: the same user the role check already resolved.
    current_user: User = Depends(get_current_user),
    detector: CatalogSimilarityDetector = Depends(get_similarity_detector),
):
    hits = await detector.find_similar(
        body.proposed_name,
        body.proposed_description,
        category_id=body.category_id,
        requesting_sp_id=current_user.id,
    )
    return [asdict(hit) for hit in hits]
