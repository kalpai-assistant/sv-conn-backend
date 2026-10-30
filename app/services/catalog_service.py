"""
Catalog service: admin-managed global service catalog.
"""

import uuid
from datetime import timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.exceptions import ServiceNotFound
from app.models.booking import Booking, BookingStatus
from app.models.booking_item import BookingItem
from app.models.category import Category
from app.models.review import Review
from app.models.service import Service
from app.models.service_relationship import RelationshipType, ServiceRelationship
from app.models.service_variant import ServiceVariant
from app.models.user import User
from app.schemas.service import (
    RelatedServiceEntry,
    RelatedServicesBucket,
    ReviewSnippet,
    ServiceDetailResponse,
    ServiceResponse,
    ServiceVariantResponse,
)

# SVC-400 (Vipul, 2026-10-29): below this many qualifying reviews a service
# shows no rating at all (None + 0) — the frontend renders "New" rather than
# an average a single unhappy customer could drag to 1 star. SVC-388: the
# same gate decides whether a card gets a review snippet.
MIN_REVIEWS_FOR_RATING = 10
# SVC-400: the rolling window behind bookings_this_week.
BOOKINGS_THIS_WEEK_WINDOW = timedelta(days=7)
# SVC-388: a snippet is a qualifying review of at least this many stars whose
# trimmed comment is longer than SNIPPET_MIN_CHARS ("good" / "nice" are out).
SNIPPET_MIN_RATING = 4
SNIPPET_MIN_CHARS = 20
# The card text is at most this many characters, the "…" included.
SNIPPET_MAX_CHARS = 80
_WHITESPACE = " \t\r\n"


def _qualifying_reviews(*columns):
    """``SELECT columns`` over the reviews that count for a service: the
    consumer's own review of a booking whose only service is
    ``booking_items.service_id``.

    A multi-service booking gets one review for the whole visit, so it can't
    be attributed to any single service; it is left out rather than counted
    against each (Vipul: an honest signal over volume). Quantity doesn't
    matter — two of the same service is one booking_item. ``reviewer_id =
    consumer_id`` keeps any non-consumer review out. The caller narrows
    ``BookingItem.service_id`` to the service(s) it wants; the rule lives
    only here, shared by the rating (SVC-400) and the snippet (SVC-388).
    """
    other_item = aliased(BookingItem)
    return (
        select(*columns)
        .select_from(Review)
        .join(Booking, Booking.id == Review.booking_id)
        .join(BookingItem, BookingItem.booking_id == Booking.id)
        .where(
            Review.reviewer_id == Booking.consumer_id,
            ~exists().where(
                other_item.booking_id == Booking.id,
                other_item.service_id != BookingItem.service_id,
            ),
        )
    )


def _snippet_text(comment: str) -> str:
    """Collapse whitespace and cut to ``SNIPPET_MAX_CHARS`` on a word
    boundary, ending in "…" (counted inside the limit)."""
    text = " ".join(comment.split())
    if len(text) <= SNIPPET_MAX_CHARS:
        return text
    cut = text[: SNIPPET_MAX_CHARS - 1]
    # Mid-word: back up to the last space. A single 79-char word is cut hard.
    if text[SNIPPET_MAX_CHARS - 1] != " " and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def _first_name(name: str | None, deleted_at) -> str | None:
    # A deleted account (SVC-367) keeps its reviews with name NULL; the
    # frontend shows its fallback label for None.
    if deleted_at is not None or not name or not name.strip():
        return None
    return name.split()[0]


class CatalogService:
    """Manages categories and provider service listings."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def list_categories(self) -> list[Category]:
        result = await self.db.execute(
            select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------

    async def list_services(
        self,
        category_id: uuid.UUID | None = None,
    ) -> list[ServiceResponse]:
        # Eager-load variants because ServiceResponse embeds them + the
        # relationship is `lazy="raise_on_sql"` — pydantic would otherwise
        # fault on serialisation, and even if it didn't, the read path would
        # fire one query per service (N+1) on a listing that can return
        # dozens of rows. `.options(selectinload(...))` fetches all variants
        # in one round-trip keyed by service_id.
        stmt = (
            select(Service)
            .where(Service.is_active.is_(True))
            .options(selectinload(Service.variants.and_(ServiceVariant.is_active.is_(True))))
        )
        if category_id:
            stmt = stmt.where(Service.category_id == category_id)
        services = list((await self.db.execute(stmt)).scalars().all())
        snippets = await self._load_review_snippets([s.id for s in services])
        return [
            ServiceResponse.model_validate(s).model_copy(
                update={"latest_review_snippet": snippets.get(s.id)}
            )
            for s in services
        ]

    async def _load_review_snippets(
        self, service_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, ReviewSnippet]:
        """SVC-388: the newest eligible review per service, in one query for
        the whole listing.

        Gated like the rating: a service with fewer than
        ``MIN_REVIEWS_FOR_RATING`` qualifying reviews (any rating, any
        comment) gets no snippet. Of the rest, eligible means at least
        ``SNIPPET_MIN_RATING`` stars and a trimmed comment longer than
        ``SNIPPET_MIN_CHARS``. ``DISTINCT ON`` keeps the newest per service,
        ``id`` breaking a created_at tie. Deleted reviewers stay in (Vipul)
        with no name. No content filter: moderation is later.
        """
        if not service_ids:
            return {}
        qualifying = (
            _qualifying_reviews(
                BookingItem.service_id.label("service_id"),
                Review.id.label("review_id"),
                Review.rating,
                Review.comment,
                Review.created_at,
                Review.reviewer_id,
            )
            .where(BookingItem.service_id.in_(service_ids))
            .cte("qualifying")
        )
        gated = (
            select(qualifying.c.service_id)
            .group_by(qualifying.c.service_id)
            .having(func.count() >= MIN_REVIEWS_FOR_RATING)
            .cte("gated")
        )
        rows = await self.db.execute(
            select(
                qualifying.c.service_id,
                qualifying.c.comment,
                qualifying.c.created_at,
                User.name,
                User.deleted_at,
            )
            .select_from(qualifying)
            .join(gated, gated.c.service_id == qualifying.c.service_id)
            .join(User, User.id == qualifying.c.reviewer_id)
            .where(
                qualifying.c.rating >= SNIPPET_MIN_RATING,
                func.length(func.btrim(qualifying.c.comment, _WHITESPACE)) > SNIPPET_MIN_CHARS,
            )
            .distinct(qualifying.c.service_id)
            .order_by(
                qualifying.c.service_id,
                qualifying.c.created_at.desc(),
                qualifying.c.review_id.desc(),
            )
        )
        return {
            row.service_id: ReviewSnippet(
                text=_snippet_text(row.comment),
                reviewer_first_name=_first_name(row.name, row.deleted_at),
                created_at=row.created_at,
            )
            for row in rows
        }

    async def create_service(
        self,
        category_id: uuid.UUID,
        name: str,
        description: str | None,
        base_price: float,
        duration_minutes: int,
    ) -> Service:
        """Admin-only: create a global catalog service."""
        service = Service(
            category_id=category_id,
            name=name,
            description=description,
            base_price=base_price,
            duration_minutes=duration_minutes,
        )
        self.db.add(service)
        await self.db.flush()
        # ServiceResponse embeds .variants, and the relationship is
        # `raise_on_sql`. A brand-new service has no variants — mark the
        # collection as loaded-empty so pydantic can serialise it without
        # tripping the sentinel. (Admin adds variants via a separate flow.)
        set_committed_value(service, "variants", [])
        return service

    async def update_service(
        self,
        service_id: uuid.UUID,
        **fields,
    ) -> Service:
        """Admin-only: update a global catalog service."""
        # Load with variants eagerly so the response can render them
        # without a raise_on_sql fault at serialisation time.
        stmt = (
            select(Service).where(Service.id == service_id).options(selectinload(Service.variants))
        )
        service = (await self.db.execute(stmt)).scalar_one_or_none()
        if not service:
            raise ServiceNotFound(service_id)
        for key, value in fields.items():
            if value is not None and hasattr(service, key):
                setattr(service, key, value)
        await self.db.flush()
        return service

    async def delete_service(self, service_id: uuid.UUID) -> None:
        """Admin-only: soft-delete by marking inactive."""
        service = await self.db.get(Service, service_id)
        if not service:
            raise ServiceNotFound(service_id)
        service.is_active = False
        await self.db.flush()

    # ------------------------------------------------------------------
    # SVC-291: single-service fetch with related-services rails prefetched
    # ------------------------------------------------------------------

    async def get_service_detail(self, service_id: uuid.UUID) -> ServiceDetailResponse:
        """One service + its variants + the three related-services buckets.

        Sits alongside ``list_services`` rather than replacing it because the
        list route (called on every home page load) doesn't need the related
        payload, and paying for it there would multiply the query count on a
        surface that's already the app's biggest single request. Fetching it
        only here keeps the list lean and the detail complete.

        The related rails are pulled in one round-trip via a join against
        ``services`` — three per-type buckets are grouped in Python from the
        single result. Inactive related services are excluded to match how
        ``list_services`` treats the catalogue: a hidden service must not
        surface anywhere the app renders it as bookable.
        """
        stmt = (
            select(Service)
            .where(Service.id == service_id, Service.is_active.is_(True))
            .options(selectinload(Service.variants.and_(ServiceVariant.is_active.is_(True))))
        )
        service = (await self.db.execute(stmt)).scalar_one_or_none()
        if not service:
            raise ServiceNotFound(service_id)

        related = await self._load_related(service_id)
        rating_average, rating_count = await self._load_rating(service_id)
        bookings_this_week = await self._count_bookings_this_week(service_id)
        return ServiceDetailResponse(
            id=service.id,
            category_id=service.category_id,
            name=service.name,
            description=service.description,
            base_price=float(service.base_price),
            duration_minutes=service.duration_minutes,
            is_active=service.is_active,
            created_at=service.created_at,
            variants=[ServiceVariantResponse.model_validate(v) for v in service.variants],
            related=related,
            banner_image_url=service.banner_image_url,
            banner_video_url=service.banner_video_url,
            steps=service.steps,
            exclusions=service.exclusions,
            hero_image_url=service.hero_image_url,
            included_items=service.included_items,
            faqs=service.faqs,
            before_after_photos=service.before_after_photos,
            rating_average=rating_average,
            rating_count=rating_count,
            bookings_this_week=bookings_this_week,
        )

    async def _load_rating(self, service_id: uuid.UUID) -> tuple[float | None, int]:
        """Average + count of the qualifying reviews (``_qualifying_reviews``)
        for this service. Below ``MIN_REVIEWS_FOR_RATING`` the answer is
        ``(None, 0)``.
        """
        row = (
            await self.db.execute(
                _qualifying_reviews(func.avg(Review.rating), func.count(Review.id)).where(
                    BookingItem.service_id == service_id
                )
            )
        ).one()
        average, count = row
        if count < MIN_REVIEWS_FOR_RATING:
            return None, 0
        return round(float(average), 1), count

    async def _count_bookings_this_week(self, service_id: uuid.UUID) -> int:
        """Distinct bookings carrying this service, created in the last
        ``BOOKINGS_THIS_WEEK_WINDOW``, that weren't cancelled or expired.

        By ``created_at``, so a reopened booking counts once. Multi-service
        bookings do count — the customer did book this service.
        """
        return (
            await self.db.execute(
                select(func.count(func.distinct(Booking.id)))
                .join(BookingItem, BookingItem.booking_id == Booking.id)
                .where(
                    BookingItem.service_id == service_id,
                    Booking.created_at >= func.now() - BOOKINGS_THIS_WEEK_WINDOW,
                    Booking.status.notin_([BookingStatus.CANCELLED, BookingStatus.EXPIRED]),
                )
            )
        ).scalar_one()

    async def _load_related(self, service_id: uuid.UUID) -> RelatedServicesBucket:
        """Fetch every edge out of `service_id` and bucket by relationship_type.

        One query, joined to ``services`` so we get name + base_price in the
        same round trip — the frontend renders the rails without a second
        request. Rows pointing at a now-inactive service are dropped in the
        WHERE clause so a soft-deleted catalogue entry never surfaces on a
        rail. Sort order comes from the seed / hand-curated overrides.
        """
        rows = await self.db.execute(
            select(ServiceRelationship, Service)
            .join(Service, Service.id == ServiceRelationship.related_service_id)
            .where(
                ServiceRelationship.source_service_id == service_id,
                Service.is_active.is_(True),
            )
            .order_by(
                ServiceRelationship.relationship_type,
                ServiceRelationship.sort_order,
                Service.name,
            )
        )

        buckets: dict[str, list[RelatedServiceEntry]] = {
            RelationshipType.HOOK.value: [],
            RelationshipType.UPSELL.value: [],
            RelationshipType.RELATED.value: [],
        }
        for edge, related_service in rows.all():
            bucket = buckets.get(edge.relationship_type)
            # Unknown relationship_type (e.g. a future 4th surface not yet in
            # the enum): drop silently rather than surfacing it on a rail the
            # frontend can't render. The DB is the source of truth for what
            # the model knows about.
            if bucket is None:
                continue
            bucket.append(
                RelatedServiceEntry(
                    id=related_service.id,
                    name=related_service.name,
                    base_price=float(related_service.base_price),
                    sort_order=edge.sort_order,
                )
            )

        return RelatedServicesBucket(
            hook=buckets[RelationshipType.HOOK.value],
            upsell=buckets[RelationshipType.UPSELL.value],
            related=buckets[RelationshipType.RELATED.value],
        )
