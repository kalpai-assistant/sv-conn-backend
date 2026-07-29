"""
Review service: create reviews and list provider reviews.
Updates provider average rating after each new review.
"""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import BookingNotFound, DuplicateReview, UnauthorizedAction
from app.models.booking import Booking, BookingStatus
from app.models.review import Review
from app.services.provider_service import ProviderService


class ReviewService:
    """Manages post-completion reviews and drives provider rating updates."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self._provider_service = ProviderService(db)

    async def create_review(
        self,
        booking_id: uuid.UUID,
        consumer_id: uuid.UUID,
        rating: int,
        comment: str | None,
    ) -> Review:
        """
        Create a review for a completed booking.
        - Only the consumer of the booking may review.
        - Only COMPLETED bookings are reviewable.
        - One review per booking (enforced by DB constraint + service check).
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)

        if booking.consumer_id != consumer_id:
            raise UnauthorizedAction("You can only review your own bookings")

        if booking.status != BookingStatus.COMPLETED:
            raise UnauthorizedAction("Booking must be completed before reviewing")

        # Check for existing review (avoid hitting DB constraint)
        existing = await self.db.execute(
            select(Review).where(Review.booking_id == booking_id)
        )
        if existing.scalar_one_or_none():
            raise DuplicateReview()

        review = Review(
            booking_id=booking_id,
            reviewer_id=consumer_id,
            reviewee_id=booking.provider_id,
            rating=rating,
            comment=comment,
        )
        self.db.add(review)
        await self.db.flush()

        # Cascade: update provider's rolling average rating
        await self._provider_service.refresh_avg_rating(booking.provider_id, rating)

        return review

    async def list_provider_reviews(
        self,
        provider_user_id: uuid.UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> dict:
        """Return paginated reviews for a provider with aggregate avg rating."""
        base_where = Review.reviewee_id == provider_user_id

        count = (
            await self.db.execute(
                select(func.count()).select_from(Review).where(base_where)
            )
        ).scalar_one()

        avg = (
            await self.db.execute(
                select(func.avg(Review.rating)).where(base_where)
            )
        ).scalar_one() or 0.0

        stmt = (
            select(Review)
            .where(base_where)
            .order_by(Review.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        reviews = list((await self.db.execute(stmt)).scalars().all())

        return {
            "reviews": reviews,
            "total": count,
            "page": page,
            "per_page": per_page,
            "avg_rating": round(float(avg), 2),
        }
