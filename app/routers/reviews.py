from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.review import CreateReviewRequest, ReviewListResponse, ReviewResponse
from app.services.review_service import ReviewService

router = APIRouter(prefix="/reviews", tags=["Reviews"])


@router.post("/", response_model=ReviewResponse)
async def create_review(
    request: CreateReviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    review = await ReviewService(db).create_review(
        booking_id=request.booking_id,
        consumer_id=current_user.id,
        rating=request.rating,
        comment=request.comment,
    )
    return review


@router.get("/provider/{provider_id}", response_model=ReviewListResponse)
async def list_provider_reviews(
    provider_id: UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    data = await ReviewService(db).list_provider_reviews(
        provider_user_id=provider_id,
        page=page,
        per_page=per_page,
    )
    return ReviewListResponse(
        reviews=[ReviewResponse.model_validate(r) for r in data["reviews"]],
        total=data["total"],
        page=data["page"],
        per_page=data["per_page"],
        avg_rating=data["avg_rating"],
    )
