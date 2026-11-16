"""
SVC-409: ``GET /services/{id}/providers``, the customer's ranked "pick your SP"
sheet.

- Listed = dispatch's own eligibility at the customer's point (no booking yet)
  AND an active ``sp_service_pricing`` row for the sheet's variant. Each
  dispatch rule failing on its own keeps the SP off.
- The variant (SVC-480): ``variant_id``, else the first active by sort_order, as
  ``create_booking`` picks it; a service with no active variant lists nobody.
  404 for a variant that isn't an active one of the service.
- The ticket's score: rating, price vs the variant's ``base_price``, distance from
  the SP's service-area centre, reliability (claims vs completions, 30 days),
  recency of the last completed job; cold-start floors; boost ×1.5.
- Weights and the sheet TTL from ``pricing_config``, cached 60 s, with a
  fallback to the seed values for a missing or bad row.
- A fixed number of statements whatever the SP count.

Every test makes its own service, so SPs other tests committed (none of which
price this service) can't appear.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import delete, event, select

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.booking_assignment_event import BookingAssignmentEvent
from app.models.booking_item import BookingItem
from app.models.booking_status_log import BookingStatusLog
from app.models.category import Category
from app.models.pricing_config import PricingConfig
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.review import Review
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.schemas.booking import CreateBookingItemRequest
from app.services import ranking_service
from app.services.booking_service import BookingService
from app.services.ranking_service import (
    DEFAULT_CONFIG,
    RankingConfig,
    is_boosted,
    price_competitiveness,
    ranking_rows_stmt,
    rating_component,
    recency_decay,
    reliability_score,
    reset_ranking_config_cache,
    why_chips,
)
from app.services.standard_variant import standard_variant
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

LAT, LNG = 12.9784, 77.6408  # the customer
HERE = f"SRID=4326;POINT({LNG} {LAT})"  # 0 km from the customer
NEAR = "SRID=4326;POINT(77.6200 12.9700)"  # ~2.4 km
FAR = "SRID=4326;POINT(77.9000 13.2000)"  # ~37 km, outside a 10 km radius
BASE_PRICE = 1000

URL = "/api/v1/services/{}/providers"


@pytest.fixture(autouse=True)
def _fresh_config_cache():
    reset_ranking_config_cache()
    yield
    reset_ranking_config_cache()


async def _service(db, *, base_price=BASE_PRICE, active=True, standard=True):
    """A service with its Standard variant at ``base_price`` (SVC-480: every
    service has one), unless ``standard`` is False."""
    slug = f"rank-{uuid.uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db.add(category)
    await db.flush()
    service = Service(
        id=uuid.uuid4(),
        category_id=category.id,
        name="Tap repair",
        base_price=base_price,
        duration_minutes=60,
        is_active=active,
    )
    db.add(service)
    if standard:
        db.add(standard_variant(service))
    await db.flush()
    return service


async def _first_variant(db, service) -> ServiceVariant:
    return await db.scalar(
        select(ServiceVariant)
        .where(ServiceVariant.service_id == service.id)
        .order_by(ServiceVariant.sort_order, ServiceVariant.id)
        .limit(1)
    )


async def _sp(
    db,
    service,
    *,
    price=BASE_PRICE,
    point=NEAR,
    radius_km=10,
    online=True,
    kyc=KYCStatus.VERIFIED,
    has_token=True,
    account_active=True,
    priced=True,
    price_active=True,
    business_name="Rakesh Plumbing",
    name="Rakesh",
    boost_eligible_at=None,
    boost_jobs=0,
    variant=None,
):
    """An SP that passes every dispatch rule for ``service`` and prices it,
    unless a knob says otherwise. Has its Pro-for-self, for claim events. The
    price is for ``variant``, else the service's first variant."""
    user = await UserFactory.create(
        db,
        role="provider",
        name=name,
        is_active=account_active,
        fcm_token=(f"fcm-{uuid.uuid4().hex[:16]}" if has_token else None),
    )
    db.add(
        ProviderProfile(
            user_id=user.id,
            kyc_status=kyc,
            is_online=online,
            service_area_center=point,
            service_area_radius_km=radius_km,
            business_name=business_name,
            boost_eligible_at=boost_eligible_at,
            boost_completed_jobs_count=boost_jobs,
        )
    )
    await db.flush()
    db.add(ProviderServiceModel(provider_id=user.id, service_id=service.id))
    db.add(
        Professional(
            service_provider_id=user.id,
            user_id=user.id,
            display_name=name,
            kyc_status=kyc,
            is_online=online,
        )
    )
    if priced:
        variant = variant or await _first_variant(db, service)
        db.add(
            SpServicePricing(
                sp_id=user.id,
                service_id=service.id,
                service_variant_id=variant.id,
                price_amount=price,
                price_unit="PER_VISIT",
                is_active=price_active,
            )
        )
    await db.flush()
    return user


async def _variant(db, service, label, price, *, sort_order=0, active=True):
    variant = ServiceVariant(
        service_id=service.id,
        label=label,
        base_price=Decimal(price),
        provider_payout=Decimal(price) * Decimal("0.7"),
        platform_commission=Decimal(price) * Decimal("0.3"),
        sort_order=sort_order,
        is_active=active,
    )
    db.add(variant)
    await db.flush()
    return variant


async def _price(db, sp, service, variant, price):
    """One more active price row for an SP ``_sp`` already made."""
    db.add(
        SpServicePricing(
            sp_id=sp.id,
            service_id=service.id,
            service_variant_id=variant.id,
            price_amount=price,
            price_unit="PER_VISIT",
        )
    )
    await db.flush()


async def _self_pro(db, sp) -> Professional:
    return await db.scalar(
        select(Professional).where(
            Professional.service_provider_id == sp.id, Professional.user_id == sp.id
        )
    )


async def _job(
    db,
    sp,
    service,
    *,
    completed=True,
    rating=None,
    days_ago=1.0,
    released=False,
    consumer_review=True,
):
    """One booking the SP claimed ``days_ago`` and, if ``completed``, finished
    then. ``released``: claimed, then given back (provider_id cleared, as a
    reopen does). ``rating``: a review of it, by the consumer unless
    ``consumer_review`` is False."""
    consumer = await UserFactory.create(db, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="1 Main St", location=HERE)
    db.add(address)
    await db.flush()
    status = (
        BookingStatus.REQUESTED
        if released
        else BookingStatus.COMPLETED
        if completed
        else BookingStatus.ACCEPTED
    )
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=None if released else sp.id,
        service_id=service.id,
        address_id=address.id,
        status=status,
    )
    at = datetime.now(UTC) - timedelta(days=days_ago)
    pro = await _self_pro(db, sp)
    db.add(
        BookingAssignmentEvent(
            booking_id=booking.id,
            kind="claimed",
            to_professional_id=pro.id,
            actor_user_id=sp.id,
            created_at=at,
        )
    )
    if released:
        db.add(
            BookingAssignmentEvent(
                booking_id=booking.id,
                kind="released",
                from_professional_id=pro.id,
                actor_user_id=sp.id,
                created_at=at + timedelta(minutes=5),
            )
        )
    if completed and not released:
        db.add(
            BookingStatusLog(
                booking_id=booking.id,
                from_status="in_progress",
                to_status="completed",
                changed_by=sp.id,
                created_at=at,
            )
        )
    if rating is not None:
        reviewer = consumer if consumer_review else sp
        db.add(
            Review(
                booking_id=booking.id,
                reviewer_id=reviewer.id,
                reviewee_id=sp.id,
                rating=rating,
            )
        )
    await db.flush()
    return booking


async def _config(db, key, value):
    await db.merge(PricingConfig(key=key, value_json=value))
    await db.flush()


async def _no_config(db):
    """No pricing_config rows (another test's committed ones included; the
    test's rollback puts them back)."""
    await db.execute(delete(PricingConfig).where(PricingConfig.key.in_(DEFAULT_CONFIG)))
    await db.flush()


@pytest.fixture
async def consumer_headers(db_session, auth_headers):
    consumer = await UserFactory.create(db_session, role="consumer")
    return auth_headers(str(consumer.id), "consumer")


async def _sheet(client, headers, service, **params):
    resp = await client.get(
        URL.format(service.id), params={"lat": LAT, "lng": LNG, **params}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(sheet) -> list[str]:
    return [p["sp_id"] for p in sheet["providers"]]


# --- shape, empty, single --------------------------------------------------------


async def test_empty_area_lists_nobody(client, db_session, consumer_headers):
    service = await _service(db_session)
    sheet = await _sheet(client, consumer_headers, service)
    assert sheet["service_id"] == str(service.id)
    assert sheet["providers"] == []


async def test_single_sp_row_has_the_ticket_shape(client, db_session, consumer_headers):
    service = await _service(db_session)
    sp = await _sp(db_session, service, price=450, point=HERE)
    before = datetime.now(UTC)
    sheet = await _sheet(client, consumer_headers, service)

    assert set(sheet) == {
        "service_id",
        "service_variant_id",
        "quoted_at",
        "ttl_seconds",
        "providers",
    }
    assert sheet["service_variant_id"] == str((await _first_variant(db_session, service)).id)
    quoted_at = datetime.fromisoformat(sheet["quoted_at"].replace("Z", "+00:00"))
    assert quoted_at.utcoffset() == timedelta(0)
    assert before - timedelta(seconds=1) <= quoted_at <= datetime.now(UTC) + timedelta(seconds=1)

    [row] = sheet["providers"]
    assert set(row) == {
        "sp_id",
        "name",
        "price_amount",
        "price_unit",
        "rating_avg",
        "rating_count",
        "distance_km",
        "reliability_score",
        "score",
        "why",
        "is_boosted",
    }
    assert row["sp_id"] == str(sp.id)
    assert row["name"] == "Rakesh Plumbing"
    # A number, not a Decimal-as-string.
    assert row["price_amount"] == 450.0 and isinstance(row["price_amount"], float)
    assert row["price_unit"] == "PER_VISIT"
    assert row["rating_avg"] is None and row["rating_count"] == 0
    assert row["distance_km"] == 0.0
    assert row["reliability_score"] == 0.8  # cold-start floor
    assert row["is_boosted"] is False


async def test_name_falls_back_to_the_sp_users_name(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _sp(db_session, service, business_name=None, name="Suresh")
    [row] = (await _sheet(client, consumer_headers, service))["providers"]
    assert row["name"] == "Suresh"


# --- who is listed --------------------------------------------------------------


async def test_sp_without_a_price_row_is_not_listed(client, db_session, consumer_headers):
    service = await _service(db_session)
    priced = await _sp(db_session, service)
    await _sp(db_session, service, priced=False)
    assert _ids(await _sheet(client, consumer_headers, service)) == [str(priced.id)]


async def test_sp_with_an_inactive_price_is_not_listed(client, db_session, consumer_headers):
    service = await _service(db_session)
    priced = await _sp(db_session, service)
    await _sp(db_session, service, price_active=False)
    assert _ids(await _sheet(client, consumer_headers, service)) == [str(priced.id)]


async def test_a_price_for_another_service_does_not_list_the_sp(
    client, db_session, consumer_headers
):
    service = await _service(db_session)
    other = await _service(db_session)
    sp = await _sp(db_session, other)  # prices `other`
    db_session.add(ProviderServiceModel(provider_id=sp.id, service_id=service.id))
    await db_session.flush()
    assert _ids(await _sheet(client, consumer_headers, service)) == []


@pytest.mark.parametrize(
    "knob",
    [
        {"online": False},
        {"has_token": False},
        {"point": FAR},
        {"kyc": KYCStatus.SUBMITTED},
        {"kyc": KYCStatus.PENDING},
        {"account_active": False},
    ],
    ids=["offline", "no_push_token", "outside_radius", "kyc_submitted", "kyc_pending", "inactive"],
)
async def test_sp_failing_a_dispatch_rule_is_not_listed(client, db_session, consumer_headers, knob):
    service = await _service(db_session)
    eligible = await _sp(db_session, service)
    await _sp(db_session, service, **knob)
    assert _ids(await _sheet(client, consumer_headers, service)) == [str(eligible.id)]


async def test_sp_not_subscribed_to_the_service_is_not_listed(client, db_session, consumer_headers):
    """A price row alone isn't enough: dispatch also needs the subscription."""
    service = await _service(db_session)
    sp = await _sp(db_session, service)
    await db_session.execute(
        delete(ProviderServiceModel).where(ProviderServiceModel.provider_id == sp.id)
    )
    await db_session.flush()
    assert _ids(await _sheet(client, consumer_headers, service)) == []


async def test_radius_is_the_sps_own(client, db_session, consumer_headers):
    service = await _service(db_session)
    wide = await _sp(db_session, service, point=FAR, radius_km=50)
    await _sp(db_session, service, point=NEAR, radius_km=2)  # 2.4 km away
    [row] = (await _sheet(client, consumer_headers, service))["providers"]
    assert row["sp_id"] == str(wide.id)
    assert 30 < row["distance_km"] < 45


# --- signals --------------------------------------------------------------------


async def test_rating_is_the_sps_consumer_reviews_across_services(
    client, db_session, consumer_headers
):
    service = await _service(db_session)
    elsewhere = await _service(db_session)
    sp = await _sp(db_session, service)
    for rating in (5, 4, 4):
        await _job(db_session, sp, elsewhere, rating=rating)
    await _job(db_session, sp, service, rating=3)
    # Not the consumer's review: left out.
    await _job(db_session, sp, service, rating=1, consumer_review=False)

    [row] = (await _sheet(client, consumer_headers, service))["providers"]
    assert row["rating_count"] == 4
    assert row["rating_avg"] == 4.0


async def test_reliability_counts_claims_and_completions_in_30_days(db_session):
    service = await _service(db_session)
    sp = await _sp(db_session, service)
    await _job(db_session, sp, service, completed=True, days_ago=2)
    await _job(db_session, sp, service, completed=True, days_ago=10)
    await _job(db_session, sp, service, completed=False, days_ago=3)
    await _job(db_session, sp, service, completed=True, days_ago=40)  # outside

    now = datetime.now(UTC)
    [row] = (
        await db_session.execute(
            ranking_rows_stmt(service.id, (await _first_variant(db_session, service)).id, HERE, now)
        )
    ).all()
    assert (row.assigned_30d, row.completed_30d) == (3, 2)
    # The newest completion, the 30-day bound doesn't apply to it.
    assert abs((now - row.last_completed_at) - timedelta(days=2)) < timedelta(minutes=1)


async def test_a_released_claim_still_counts_against_the_sp(client, db_session, consumer_headers):
    service = await _service(db_session)
    sp = await _sp(db_session, service)
    await _job(db_session, sp, service, released=True)

    now = datetime.now(UTC)
    [row] = (
        await db_session.execute(
            ranking_rows_stmt(service.id, (await _first_variant(db_session, service)).id, HERE, now)
        )
    ).all()
    # One claim, no completion row at all (the outer join's NULL is 0).
    assert (row.assigned_30d, row.completed_30d) == (1, None)

    # Past the floor, it shows: 2 completed out of 3 claimed.
    await _job(db_session, sp, service)
    await _job(db_session, sp, service)
    [listed] = (await _sheet(client, consumer_headers, service))["providers"]
    assert listed["reliability_score"] == 0.67


async def test_another_sps_jobs_do_not_count(db_session):
    service = await _service(db_session)
    sp = await _sp(db_session, service)
    other = await _sp(db_session, service)
    for _ in range(3):
        await _job(db_session, other, service, rating=5)
    rows = {
        r.sp_id: r
        for r in (
            await db_session.execute(
                ranking_rows_stmt(
                    service.id,
                    (await _first_variant(db_session, service)).id,
                    HERE,
                    datetime.now(UTC),
                )
            )
        ).all()
    }
    mine = rows[sp.id]
    assert (mine.rating_count, mine.assigned_30d, mine.completed_30d) == (None, None, None)
    assert (rows[other.id].rating_count, rows[other.id].assigned_30d) == (3, 3)


# --- ordering -------------------------------------------------------------------


async def test_cheaper_than_base_price_ranks_higher_and_says_below_market(
    client, db_session, consumer_headers
):
    service = await _service(db_session)
    at_base = await _sp(db_session, service, price=1000)
    below = await _sp(db_session, service, price=850)
    above = await _sp(db_session, service, price=1300)
    sheet = await _sheet(client, consumer_headers, service)

    assert _ids(sheet) == [str(below.id), str(at_base.id), str(above.id)]
    chips = {p["sp_id"]: p["why"] for p in sheet["providers"]}
    assert "Below market" in chips[str(below.id)]
    assert "Below market" not in chips[str(at_base.id)]


async def test_below_market_is_at_or_under_90_percent(client, db_session, consumer_headers):
    service = await _service(db_session)
    edge = await _sp(db_session, service, price=900)
    over = await _sp(db_session, service, price=901)
    chips = {
        p["sp_id"]: p["why"] for p in (await _sheet(client, consumer_headers, service))["providers"]
    }
    assert "Below market" in chips[str(edge.id)]
    assert "Below market" not in chips[str(over.id)]


async def test_price_competitiveness_is_capped(client, db_session, consumer_headers):
    """A price near zero scores 1.5 on price, no more: two absurd prices tie."""
    service = await _service(db_session)
    await _config(
        db_session,
        "ranking_weights",
        {"rating": 0, "price_vs_ref": 1, "distance": 0, "reliability": 0, "activity": 0},
    )
    await _sp(db_session, service, price=1)
    await _sp(db_session, service, price=400)
    scores = [p["score"] for p in (await _sheet(client, consumer_headers, service))["providers"]]
    assert scores == [1.5, 1.5]


async def test_equal_scores_come_back_in_sp_id_order(client, db_session, consumer_headers):
    service = await _service(db_session)
    sps = [await _sp(db_session, service) for _ in range(4)]
    assert _ids(await _sheet(client, consumer_headers, service)) == sorted(str(s.id) for s in sps)


async def test_limit_keeps_the_best(client, db_session, consumer_headers):
    service = await _service(db_session)
    best = await _sp(db_session, service, price=800)
    second = await _sp(db_session, service, price=900)
    await _sp(db_session, service, price=1000)
    sheet = await _sheet(client, consumer_headers, service, limit=2)
    assert _ids(sheet) == [str(best.id), str(second.id)]


async def _established(db, service, rating, *, point=NEAR):
    """An SP with 6 consumer reviews of ``rating`` and 6/6 recent completions."""
    sp = await _sp(db, service, point=point)
    for _ in range(6):
        await _job(db, sp, service, rating=rating)
    return sp


async def test_cold_start_floors_keep_a_new_sp_off_the_bottom(client, db_session, consumer_headers):
    """No reviews (rating 0.6, not 0) and no claims (reliability 0.8, not 0): a
    new SP beats an established one with poor signals."""
    service = await _service(db_session)
    new = await _sp(db_session, service)
    poor = await _sp(db_session, service)
    for _ in range(5):
        await _job(db_session, poor, service, rating=1, completed=False)
    sheet = await _sheet(client, consumer_headers, service)
    assert _ids(sheet) == [str(new.id), str(poor.id)]
    rows = {p["sp_id"]: p for p in sheet["providers"]}
    assert rows[str(new.id)]["reliability_score"] == 0.8
    assert rows[str(poor.id)]["reliability_score"] == 0.0


async def test_rating_floor_applies_below_five_reviews(client, db_session, consumer_headers):
    """Four 5-star reviews still score the neutral 0.6, the same as none."""
    service = await _service(db_session)
    await _config(
        db_session,
        "ranking_weights",
        {"rating": 1, "price_vs_ref": 0, "distance": 0, "reliability": 0, "activity": 0},
    )
    four = await _sp(db_session, service)
    for _ in range(4):
        await _job(db_session, four, service, rating=5)
    five = await _sp(db_session, service)
    for _ in range(5):
        await _job(db_session, five, service, rating=5)
    rows = {p["sp_id"]: p for p in (await _sheet(client, consumer_headers, service))["providers"]}
    assert rows[str(four.id)]["score"] == 0.6
    assert rows[str(four.id)]["rating_avg"] == 5.0
    assert rows[str(five.id)]["score"] == 1.0


async def test_a_boosted_new_sp_lands_in_the_top_half(client, db_session, consumer_headers):
    service = await _service(db_session)
    field = [await _established(db_session, service, rating) for rating in (5, 4, 4, 3, 3)]
    unboosted_new = await _sp(db_session, service)
    boosted_new = await _sp(
        db_session, service, boost_eligible_at=datetime.now(UTC) - timedelta(days=2)
    )
    sheet = await _sheet(client, consumer_headers, service)
    ids = _ids(sheet)
    assert len(ids) == len(field) + 2
    assert ids.index(str(boosted_new.id)) < len(ids) / 2
    # The same SP without the boost sits in the bottom half: the boost moved it.
    assert ids.index(str(unboosted_new.id)) >= len(ids) / 2
    row = next(p for p in sheet["providers"] if p["sp_id"] == str(boosted_new.id))
    assert row["is_boosted"] is True and "New on platform" in row["why"]


async def test_a_boosted_but_poor_sp_does_not_beat_a_great_one(
    client, db_session, consumer_headers
):
    """×1.5, not a pin: boost doesn't carry bad ratings, a high price and
    distance past a strong established SP."""
    service = await _service(db_session)
    great = await _established(db_session, service, 5, point=HERE)
    poor = await _sp(
        db_session,
        service,
        price=1500,
        point="SRID=4326;POINT(77.7100 12.9784)",  # ~7.5 km
        boost_eligible_at=datetime.now(UTC) - timedelta(days=1),
    )
    for _ in range(6):
        await _job(db_session, poor, service, rating=2)
    assert _ids(await _sheet(client, consumer_headers, service)) == [str(great.id), str(poor.id)]


@pytest.mark.parametrize(
    ("started_days_ago", "jobs", "expected"),
    [
        (None, 0, False),  # never started
        (1, 0, True),
        (13.9, 9, True),
        (14.1, 0, False),  # window over
        (1, 10, False),  # jobs used up
        (-1, 0, False),  # starts tomorrow
    ],
)
def test_boost_window(started_days_ago, jobs, expected):
    now = datetime.now(UTC)
    config = RankingConfig(DEFAULT_CONFIG["ranking_weights"], 600, 14, 10)
    started = None if started_days_ago is None else now - timedelta(days=started_days_ago)
    assert is_boosted(started, jobs, config, now) is expected


async def test_boost_window_comes_from_config(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _config(db_session, "boost_window_days", 3)
    await _config(db_session, "boost_window_jobs", 2)
    started = datetime.now(UTC) - timedelta(days=5)
    sp_old = await _sp(db_session, service, boost_eligible_at=started)
    sp_busy = await _sp(
        db_session, service, boost_eligible_at=datetime.now(UTC) - timedelta(days=1), boost_jobs=2
    )
    sp_on = await _sp(
        db_session, service, boost_eligible_at=datetime.now(UTC) - timedelta(days=1), boost_jobs=1
    )
    rows = {
        p["sp_id"]: p["is_boosted"]
        for p in (await _sheet(client, consumer_headers, service))["providers"]
    }
    assert rows == {str(sp_old.id): False, str(sp_busy.id): False, str(sp_on.id): True}


# --- config ---------------------------------------------------------------------

PRICE_ONLY = {"rating": 0, "price_vs_ref": 1, "distance": 0, "reliability": 0, "activity": 0}
# A new SP, priced at base, 0 km away, under the seed weights:
# 0.35*0.6 + 0.25*1 + 0.20*1 + 0.15*0.8 + 0.05*0
DEFAULT_SCORE_AT_BASE_HERE = 0.78


async def test_weights_come_from_pricing_config(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _config(db_session, "ranking_weights", PRICE_ONLY)
    await _sp(db_session, service, price=800, point=HERE)
    [row] = (await _sheet(client, consumer_headers, service))["providers"]
    assert row["score"] == 1.2  # 1 - (800 - 1000) / 1000


async def test_weights_are_cached_for_60_seconds(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _config(db_session, "ranking_weights", PRICE_ONLY)
    await _sp(db_session, service, price=800, point=HERE)
    clock = [1000.0]
    with patch.object(ranking_service, "monotonic", side_effect=lambda: clock[0]):
        assert (await _sheet(client, consumer_headers, service))["providers"][0]["score"] == 1.2

        await _config(
            db_session,
            "ranking_weights",
            {"rating": 1, "price_vs_ref": 0, "distance": 0, "reliability": 0, "activity": 0},
        )
        clock[0] += 59
        assert (await _sheet(client, consumer_headers, service))["providers"][0]["score"] == 1.2

        clock[0] += 2
        assert (await _sheet(client, consumer_headers, service))["providers"][0]["score"] == 0.6


@pytest.mark.parametrize(
    "bad",
    [
        None,  # row missing
        {"rating": 1},  # keys missing
        {**PRICE_ONLY, "extra": 0},
        {"rating": 0.5, "price_vs_ref": 0.5, "distance": 0.5, "reliability": 0, "activity": 0},
        {"rating": "1", "price_vs_ref": 0, "distance": 0, "reliability": 0, "activity": 0},
        [0.35, 0.25, 0.2, 0.15, 0.05],
    ],
    ids=["missing", "keys_missing", "extra_key", "sum_not_one", "not_a_number", "not_an_object"],
)
async def test_bad_or_missing_weights_fall_back_to_the_seed(
    client, db_session, consumer_headers, caplog, bad
):
    service = await _service(db_session)
    await _no_config(db_session)
    if bad is not None:
        await _config(db_session, "ranking_weights", bad)
    await _sp(db_session, service, point=HERE)
    with caplog.at_level("WARNING", logger="app.services.ranking_service"):
        [row] = (await _sheet(client, consumer_headers, service))["providers"]
    assert row["score"] == DEFAULT_SCORE_AT_BASE_HERE
    assert any(
        "ranking.config_fallback key=ranking_weights" in r.getMessage() for r in caplog.records
    )


async def test_ttl_seconds_comes_from_pricing_config(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _config(db_session, "sheet_ttl_seconds", 120)
    assert (await _sheet(client, consumer_headers, service))["ttl_seconds"] == 120


@pytest.mark.parametrize("bad", [None, 0, "600", 1.5, True])
async def test_bad_or_missing_ttl_falls_back_to_600(client, db_session, consumer_headers, bad):
    service = await _service(db_session)
    await _no_config(db_session)
    if bad is not None:
        await _config(db_session, "sheet_ttl_seconds", bad)
    assert (await _sheet(client, consumer_headers, service))["ttl_seconds"] == 600


# --- why chips ------------------------------------------------------------------


def _chips(**overrides):
    values = {
        "rating_avg": None,
        "rating_count": 0,
        "distance_km": 5.0,
        "boosted": False,
        "reliability": 0.8,
        "price": 1000.0,
        "reference": 1000.0,
        "activity": 0.0,
    }
    values.update(overrides)
    return why_chips(**values)


def test_no_chip_fits_a_plain_sp():
    assert _chips() == []


@pytest.mark.parametrize(
    ("overrides", "chip"),
    [
        ({"rating_avg": 4.5, "rating_count": 20}, "Highly rated"),
        ({"distance_km": 1.2}, "1.2 km away"),
        ({"boosted": True}, "New on platform"),
        ({"reliability": 0.95}, "Reliable"),
        ({"price": 900.0}, "Below market"),
        ({"activity": 0.91}, "Active today"),
    ],
)
def test_each_chip_rule(overrides, chip):
    assert _chips(**overrides) == [chip]


@pytest.mark.parametrize(
    "overrides",
    [
        {"rating_avg": 4.49, "rating_count": 100},
        {"rating_avg": 5.0, "rating_count": 19},
        {"distance_km": 2.0},
        {"reliability": 0.94},
        {"price": 901.0},
        {"activity": 0.9},
    ],
)
def test_each_chip_rule_edge_misses(overrides):
    assert _chips(**overrides) == []


def test_at_most_three_chips_in_rule_order():
    every = {
        "rating_avg": 4.9,
        "rating_count": 50,
        "distance_km": 0.4,
        "boosted": True,
        "reliability": 1.0,
        "price": 500.0,
        "activity": 1.0,
    }
    assert _chips(**every) == ["Highly rated", "0.4 km away", "New on platform"]
    del every["rating_avg"], every["boosted"]
    assert _chips(**every) == ["0.4 km away", "Reliable", "Below market"]


async def test_chips_on_the_wire(client, db_session, consumer_headers):
    """Recent completions + near + cheap, through the endpoint."""
    service = await _service(db_session)
    sp = await _sp(db_session, service, price=850, point=HERE)
    for _ in range(3):
        await _job(db_session, sp, service, days_ago=0.1)
    [row] = (await _sheet(client, consumer_headers, service))["providers"]
    assert row["sp_id"] == str(sp.id)
    assert row["why"] == ["0.0 km away", "Reliable", "Below market"]


# --- the formula's pieces -------------------------------------------------------


def test_formula_pieces():
    assert price_competitiveness(1000, 1000) == 1.0
    assert price_competitiveness(2500, 1000) == 0.0
    assert price_competitiveness(0, 1000) == 1.5
    assert price_competitiveness(500, 0) == 1.0  # no reference: neutral
    assert rating_component(None, 0) == 0.6
    assert rating_component(4.0, 5) == 0.8
    assert reliability_score(0, 2) == 0.8  # floor
    assert reliability_score(3, 3) == 1.0
    assert reliability_score(5, 3) == 1.0  # capped
    now = datetime.now(UTC)
    assert recency_decay(None, now) == 0.0
    assert recency_decay(now, now) == 1.0
    assert 0.36 < recency_decay(now - timedelta(days=14), now) < 0.37


# --- variants (SVC-480) ---------------------------------------------------------


async def _bhk_service(db):
    """Deep cleaning: 1 BHK ₹2,499 (first by sort_order), 2 BHK ₹2,999, and an
    inactive "Studio" ordered before both. services.base_price is the cheapest."""
    service = await _service(db, base_price=2499, standard=False)
    studio = await _variant(db, service, "Studio", 1999, sort_order=0, active=False)
    two = await _variant(db, service, "2 BHK", 2999, sort_order=2)
    one = await _variant(db, service, "1 BHK", 2499, sort_order=1)
    return service, studio, one, two


async def test_a_variant_lists_that_variants_prices(client, db_session, consumer_headers):
    service, _, one, two = await _bhk_service(db_session)
    both = await _sp(db_session, service, variant=one, price=2400)
    await _price(db_session, both, service, two, 2800)
    only_two = await _sp(db_session, service, variant=two, price=2900)

    sheet = await _sheet(client, consumer_headers, service, variant_id=str(two.id))

    assert sheet["service_variant_id"] == str(two.id)
    # One row per SP: its 2 BHK price, never its 1 BHK one too.
    prices = sorted((p["sp_id"], p["price_amount"]) for p in sheet["providers"])
    assert prices == sorted([(str(both.id), 2800.0), (str(only_two.id), 2900.0)])


async def test_an_sp_priced_only_for_another_variant_is_not_listed(
    client, db_session, consumer_headers
):
    service, _, one, two = await _bhk_service(db_session)
    one_only = await _sp(db_session, service, variant=one)
    two_only = await _sp(db_session, service, variant=two)

    assert _ids(await _sheet(client, consumer_headers, service, variant_id=str(one.id))) == [
        str(one_only.id)
    ]
    assert _ids(await _sheet(client, consumer_headers, service, variant_id=str(two.id))) == [
        str(two_only.id)
    ]


async def test_no_variant_id_uses_the_first_active_variant_as_a_booking_would(
    client, db_session, consumer_headers
):
    """The inactive Studio is first by sort_order, so 1 BHK is the default, and
    create_booking bills a line without a variant for the same one."""
    service, studio, one, two = await _bhk_service(db_session)
    on_one = await _sp(db_session, service, variant=one, price=2400)
    await _sp(db_session, service, variant=two)
    await _sp(db_session, service, variant=studio)  # active price, inactive variant

    sheet = await _sheet(client, consumer_headers, service)

    assert sheet["service_variant_id"] == str(one.id)
    assert _ids(sheet) == [str(on_one.id)]
    assert sheet["providers"][0]["price_amount"] == 2400.0

    consumer = await UserFactory.create(db_session, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="1 Main St", location=HERE)
    db_session.add(address)
    await db_session.flush()
    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[CreateBookingItemRequest(service_id=service.id)],
        address_id=address.id,
    )
    [item] = (
        await db_session.scalars(select(BookingItem).where(BookingItem.booking_id == booking.id))
    ).all()
    assert str(item.service_variant_id) == sheet["service_variant_id"]


async def test_the_reference_price_is_the_variants(client, db_session, consumer_headers):
    """₹2,700 is above services.base_price (₹2,499) but at 90% of the 2 BHK's
    ₹2,999 or under it: "Below market", and it outranks a ₹2,999 SP."""
    service, _, _, two = await _bhk_service(db_session)
    cheap = await _sp(db_session, service, variant=two, price=2699)
    at_ref = await _sp(db_session, service, variant=two, price=2999)

    sheet = await _sheet(client, consumer_headers, service, variant_id=str(two.id))

    assert _ids(sheet) == [str(cheap.id), str(at_ref.id)]
    chips = {p["sp_id"]: p["why"] for p in sheet["providers"]}
    assert "Below market" in chips[str(cheap.id)]
    assert "Below market" not in chips[str(at_ref.id)]


async def test_a_service_whose_variants_are_all_off_lists_nobody(
    client, db_session, consumer_headers
):
    """No variant to price, so nobody is listed: 200, no variant, no rows,
    even for an SP whose price row is still active."""
    service = await _service(db_session, standard=False)
    retired = await _variant(db_session, service, "Old", 2000, active=False)
    await _sp(db_session, service, variant=retired, price=1700)

    sheet = await _sheet(client, consumer_headers, service)

    assert (sheet["service_variant_id"], sheet["providers"]) == (None, [])
    assert sheet["ttl_seconds"] > 0  # the config is still read


async def test_a_service_with_no_variant_row_lists_nobody(client, db_session, consumer_headers):
    """Only a row written outside the admin paths can have none."""
    service = await _service(db_session, standard=False)
    sheet = await _sheet(client, consumer_headers, service)
    assert (sheet["service_variant_id"], sheet["providers"]) == (None, [])


async def _variant_404(client, headers, service, variant_id):
    resp = await client.get(
        URL.format(service.id),
        params={"lat": LAT, "lng": LNG, "variant_id": str(variant_id)},
        headers=headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "VARIANT_NOT_FOUND"


@pytest.mark.parametrize("which", ["inactive", "another_services", "unknown"])
async def test_a_variant_that_isnt_an_active_one_of_the_service_is_404(
    client, db_session, consumer_headers, which
):
    service, studio, one, _ = await _bhk_service(db_session)
    await _sp(db_session, service, variant=one)
    other = await _service(db_session)
    foreign = await _variant(db_session, other, "1 BHK", 2499)
    await _sp(db_session, other, variant=foreign)
    variant_id = {
        "inactive": studio.id,
        "another_services": foreign.id,
        "unknown": uuid.uuid4(),
    }[which]
    await _variant_404(client, consumer_headers, service, variant_id)


async def test_a_variant_id_for_a_service_with_no_active_variant_is_404(
    client, db_session, consumer_headers
):
    service = await _service(db_session, standard=False)
    retired = await _variant(db_session, service, "Old", 2000, active=False)
    await _sp(db_session, service, variant=retired)
    await _variant_404(client, consumer_headers, service, retired.id)
    await _variant_404(client, consumer_headers, service, uuid.uuid4())


async def test_an_unknown_service_with_a_variant_id_is_still_service_not_found(
    client, consumer_headers
):
    resp = await client.get(
        URL.format(uuid.uuid4()),
        params={"lat": LAT, "lng": LNG, "variant_id": str(uuid.uuid4())},
        headers=consumer_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "SERVICE_NOT_FOUND"


async def test_a_bad_variant_id_is_422(client, db_session, consumer_headers):
    service = await _service(db_session)
    resp = await client.get(
        URL.format(service.id),
        params={"lat": LAT, "lng": LNG, "variant_id": "1-bhk"},
        headers=consumer_headers,
    )
    assert resp.status_code == 422


# --- errors ---------------------------------------------------------------------


async def test_unknown_service_is_404(client, consumer_headers):
    resp = await client.get(
        URL.format(uuid.uuid4()), params={"lat": LAT, "lng": LNG}, headers=consumer_headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "SERVICE_NOT_FOUND"


async def test_inactive_service_is_404(client, db_session, consumer_headers):
    service = await _service(db_session, active=False)
    await _sp(db_session, service)
    resp = await client.get(
        URL.format(service.id), params={"lat": LAT, "lng": LNG}, headers=consumer_headers
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"lat": 91, "lng": LNG},
        {"lat": -91, "lng": LNG},
        {"lat": LAT, "lng": 181},
        {"lat": LAT, "lng": -181},
        {"lat": "nan", "lng": LNG},
        {"lat": "north", "lng": LNG},
        {"lng": LNG},
        {"lat": LAT},
        {"lat": LAT, "lng": LNG, "limit": 0},
        {"lat": LAT, "lng": LNG, "limit": 51},
    ],
)
async def test_bad_query_is_422(client, db_session, consumer_headers, params):
    service = await _service(db_session)
    resp = await client.get(URL.format(service.id), params=params, headers=consumer_headers)
    assert resp.status_code == 422


async def test_needs_a_login(client, db_session):
    service = await _service(db_session)
    resp = await client.get(URL.format(service.id), params={"lat": LAT, "lng": LNG})
    assert resp.status_code == 401


async def test_a_provider_can_read_it_too(client, db_session, auth_headers):
    service = await _service(db_session)
    sp = await _sp(db_session, service)
    sheet = await _sheet(client, auth_headers(str(sp.id), "provider"), service)
    assert _ids(sheet) == [str(sp.id)]


# --- performance ----------------------------------------------------------------


async def test_statement_count_is_the_same_for_1_and_100_sps(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _config(db_session, "ranking_weights", DEFAULT_CONFIG["ranking_weights"])
    first = await _sp(db_session, service)
    await _job(db_session, first, service, rating=5)

    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    async def run() -> dict[str, int]:
        statements.clear()
        sheet = await _sheet(client, consumer_headers, service)
        return {
            "listed": len(sheet["providers"]),
            "total": len(statements),
            "ranking": sum("sp_service_pricing" in s for s in statements),
            "config": sum("FROM pricing_config" in s for s in statements),
        }

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        reset_ranking_config_cache()
        cold_one = await run()
        warm_one = await run()
        for i in range(99):
            sp = await _sp(db_session, service, price=900 + i)
            if i % 3 == 0:
                await _job(db_session, sp, service, rating=4)
        reset_ranking_config_cache()
        cold_hundred = await run()
        warm_hundred = await run()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert (cold_one["listed"], cold_hundred["listed"]) == (1, 50)  # capped at the limit
    # Whatever the SP count: one ranking statement, the config read once a
    # minute, and the same total (login + service lookup + ranking [+ config]).
    assert cold_one == {**cold_hundred, "listed": 1}
    assert warm_one == {**warm_hundred, "listed": 1}
    assert (cold_one["ranking"], cold_one["config"]) == (1, 1)
    assert (warm_one["ranking"], warm_one["config"]) == (1, 0)
    assert warm_one["total"] == cold_one["total"] - 1
