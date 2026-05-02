from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime


class CreateReviewRequest(BaseModel):
    booking_id: UUID
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(None, max_length=1000)


class ReviewResponse(BaseModel):
    id: UUID
    booking_id: UUID
    reviewer_id: UUID
    reviewee_id: UUID
    rating: int
    comment: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReviewListResponse(BaseModel):
    reviews: list[ReviewResponse]
    total: int
    page: int
    per_page: int
    avg_rating: float
