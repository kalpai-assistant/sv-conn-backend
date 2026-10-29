"""Coverage for the SVC-340 steps + exclusions columns.

Both are nullable JSONB lists, shown only on the service detail page. The
invariants: the detail route returns them in order when set and present-and-null
when unset (the frontend hides the section on null), and the list route leaves
them out so the home-page payload stays lean.

The seed text lives in ``app.seed_data.service_details``; the seed scripts apply
it after creating services, and the workbook must still name every seeded service.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.models.category import Category
from app.models.service import Service
from app.schemas.service import ServiceDetailResponse, ServiceStep
from app.seed_data import service_details
from app.seed_data.service_details import (
    COMMON_FAQS,
    SERVICE_DETAILS,
    apply_service_details,
    service_details_params,
    svc_340_details_params,
)
from scripts.seed_pricing_from_xlsx import DEFAULT_XLSX, _slug, parse_workbook


async def _make_service(db_session, **overrides):
    suffix = uuid4().hex[:6]
    cat = Category(name=f"Cat {suffix}", slug=f"cat-{suffix}", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    service = Service(
        category_id=cat.id,
        name=f"Svc {suffix}",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
        **overrides,
    )
    db_session.add(service)
    await db_session.flush()
    return service


@pytest.mark.asyncio
async def test_get_service_detail_returns_steps_and_exclusions_in_order(client, db_session):
    steps = ["Arrives with tools", "Inspects the area", "Does the work", "Cleans up"]
    exclusions = ["Does not include spare parts", "Does not include civil work"]
    service = await _make_service(db_session, steps=steps, exclusions=exclusions)

    response = await client.get(f"/api/v1/services/{service.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["steps"] == steps
    assert body["exclusions"] == exclusions


@pytest.mark.asyncio
async def test_get_service_detail_returns_null_when_unset(client, db_session):
    """Present-and-null, not missing: null is how the frontend knows to hide
    the section."""
    service = await _make_service(db_session)

    response = await client.get(f"/api/v1/services/{service.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "steps" in body and body["steps"] is None
    assert "exclusions" in body and body["exclusions"] is None


@pytest.mark.asyncio
async def test_list_services_leaves_out_steps_and_exclusions(client, db_session):
    service = await _make_service(
        db_session, steps=["Arrives with tools"], exclusions=["Does not include parts"]
    )

    response = await client.get(f"/api/v1/services?category_id={service.category_id}")

    assert response.status_code == 200, response.text
    matched = [s for s in response.json() if s["id"] == str(service.id)]
    assert matched, "seeded service missing from list response"
    assert "steps" not in matched[0]
    assert "exclusions" not in matched[0]


# ---------------------------------------------------------------------------
# Seed data (app/seed_data/service_details.py)
# ---------------------------------------------------------------------------


def test_seed_data_is_ten_services_in_ticket_shape():
    assert len(SERVICE_DETAILS) == 10
    assert len({slug for slug, _ in SERVICE_DETAILS}) == 10, "one service per sub-category"
    for key, details in SERVICE_DETAILS.items():
        assert set(details) == {"steps", "exclusions", "included_items", "faqs"}, key
        assert 4 <= len(details["steps"]) <= 6, key
        assert 2 <= len(details["exclusions"]) <= 4, key
        titles = [step["title"] for step in details["steps"]]
        texts = titles + details["exclusions"] + details["included_items"]
        assert all(isinstance(s, str) and s.strip() for s in texts), key


def test_every_seeded_service_validates_as_a_detail_response():
    """What the UPDATE writes must parse as ServiceDetailResponse, or GET
    /services/{id} 500s for that service — e.g. a mixed string/object steps
    list, a step without a title, or a FAQ missing its answer."""
    base = {
        "id": uuid4(),
        "category_id": uuid4(),
        "name": "x",
        "description": None,
        "base_price": 1.0,
        "duration_minutes": 60,
        "is_active": True,
        "created_at": datetime.now(UTC),
    }
    for params in service_details_params():
        content = {
            col: json.loads(params[col])
            for col in ("steps", "exclusions", "included_items", "faqs")
        }
        parsed = ServiceDetailResponse.model_validate({**base, **content})
        key = (params["category_slug"], params["name"])
        assert all(isinstance(step, ServiceStep) for step in parsed.steps), key
        assert parsed.included_items and parsed.faqs, key
        assert parsed.hero_image_url is None and parsed.before_after_photos is None, key


def test_steps_have_one_shape_per_service_and_known_icons():
    """Durations are all-or-nothing per service (the four untimed services
    carry none), and icons stay within the keywords the frontend maps."""
    known_icons = {
        "search", "inspect", "wind", "dust", "bath", "bathroom", "kitchen",
        "degrease", "sparkles", "mop", "finish", "review",
    }  # fmt: skip
    untimed = {"ac", "pest-control", "appliances", "painting"}
    for (slug, name), details in SERVICE_DETAILS.items():
        steps = details["steps"]
        assert all(set(step) <= {"title", "duration_minutes", "icon"} for step in steps), name
        assert all(step["icon"] in known_icons for step in steps), name
        timed = ["duration_minutes" in step for step in steps]
        assert all(timed) or not any(timed), name
        assert any(timed) == (slug not in untimed), name


def test_common_faqs_are_appended_once_after_each_services_own():
    for params in service_details_params():
        faqs = json.loads(params["faqs"])
        own = SERVICE_DETAILS[(params["category_slug"], params["name"])]["faqs"]
        assert faqs == own + COMMON_FAQS, params["name"]
        for common in COMMON_FAQS:
            assert faqs.count(common) == 1, (params["name"], common["question"])
    # Written once: no service lists a shared question among its own.
    common_questions = {faq["question"] for faq in COMMON_FAQS}
    for details in SERVICE_DETAILS.values():
        assert not common_questions & {faq["question"] for faq in details["faqs"]}


def test_svc_340_params_keep_the_step_titles_as_strings():
    """The frozen SVC-340 statement (and the SVC-400 content downgrade) writes
    the titles as plain strings, the shape SVC-340 shipped."""
    for params in svc_340_details_params():
        details = SERVICE_DETAILS[(params["category_slug"], params["name"])]
        assert json.loads(params["steps"]) == [step["title"] for step in details["steps"]]
        assert set(params) == {"category_slug", "name", "steps", "exclusions"}


@pytest.mark.skipif(not DEFAULT_XLSX.exists(), reason="pricing workbook not checked out")
def test_every_seeded_service_is_in_the_pricing_workbook():
    """The pricing re-seed recreates services from the workbook. A key it no
    longer names would lose its text silently, so a rename must fail here."""
    workbook = {(_slug(r.category_name), r.service_name) for r in parse_workbook(DEFAULT_XLSX)}
    assert set(SERVICE_DETAILS) <= workbook, sorted(set(SERVICE_DETAILS) - workbook)


async def _get_or_make_category(db_session, slug: str) -> Category:
    cat = (
        await db_session.execute(select(Category).where(Category.slug == slug))
    ).scalar_one_or_none()
    if cat is None:
        cat = Category(name=f"SVC-340 {slug}", slug=slug, is_active=True, sort_order=1)
        db_session.add(cat)
        await db_session.flush()
    return cat


@pytest.mark.asyncio
async def test_apply_service_details_fills_new_services_and_reports_unmatched_keys(
    db_session, monkeypatch
):
    """What the seed scripts do: create services, then apply. The services are
    left pending (not flushed), as the scripts leave them. A key with no service
    is reported, not skipped silently."""
    missing_key = ("svc-340-nowhere", "No such service")
    monkeypatch.setattr(
        service_details,
        "SERVICE_DETAILS",
        {
            **SERVICE_DETAILS,
            missing_key: {
                "steps": [{"title": "Step one"}],
                "exclusions": ["Not this"],
                "included_items": ["This"],
                "faqs": [],
            },
        },
    )
    plumber = await _get_or_make_category(db_session, "plumber")
    salon_men = await _get_or_make_category(db_session, "salon-men")
    drain = Service(
        category_id=plumber.id, name="Drain blockage removal", base_price=99, is_active=True
    )
    haircut = Service(
        category_id=salon_men.id, name="Men's haircut", base_price=259, is_active=True
    )
    other = Service(category_id=salon_men.id, name="Beard styling", base_price=149, is_active=True)
    db_session.add_all([drain, haircut, other])

    unmatched = await apply_service_details(db_session)

    assert missing_key in unmatched
    assert ("plumber", "Drain blockage removal") not in unmatched
    assert ("salon-men", "Men's haircut") not in unmatched
    rows = await db_session.execute(
        text(
            "SELECT id, steps, exclusions, included_items, faqs FROM services "
            "WHERE id IN (:a, :b, :c)"
        ),
        {"a": drain.id, "b": haircut.id, "c": other.id},
    )
    details = {row.id: (row.steps, row.exclusions, row.included_items, row.faqs) for row in rows}

    def expected(key):
        d = SERVICE_DETAILS[key]
        return d["steps"], d["exclusions"], d["included_items"], d["faqs"] + COMMON_FAQS

    assert details[drain.id] == expected(("plumber", "Drain blockage removal"))
    assert details[haircut.id] == expected(("salon-men", "Men's haircut"))
    assert details[other.id] == (None, None, None, None)
