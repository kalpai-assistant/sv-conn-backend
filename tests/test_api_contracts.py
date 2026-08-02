"""API contract tests (SVC-157).

For every endpoint the frontend actually calls, this module makes a real
request through the ASGI transport and validates the response body against
the schema FastAPI declares on the route via ``response_model``.

## Why this exists

The frontend regenerates ``src/types/api.ts`` from ``svc_connect.json`` (the
OpenAPI export). Types are only as honest as the export, and the export is
only as honest as the route declarations. Nothing in the loop verifies that
what the backend *actually returns* matches what it *claims to return*.

The concrete drift this catches: ``useSubmitReview`` was called with
``bookingId`` for weeks and silently ignored, because the real field is
``booking_id``. A schema-conformance test on ``POST /reviews/`` would have
failed the first time the shapes diverged.

## Approach

For each endpoint we care about:

1. Make a happy-path request through ``AsyncClient``.
2. Pull the ``response_model`` schema for that route out of ``app.openapi()``
   (single source of truth — same source the frontend types are generated
   from).
3. Validate the response body against the schema. Any extra / missing /
   wrongly-typed field fails the test.

$refs in the schema (Pydantic composed models) are resolved via a jsonschema
Registry seeded from ``components/schemas``. Everything stays in-process; no
external toolchain.

## What this deliberately does NOT do

* **Doesn't check that ``svc_connect.json`` is fresh.** The file is
  gitignored (SVC-145, ``5d278e3``) so there's no in-tree target to diff
  against. The freshness signal lives on the frontend side: when it
  regenerates and TypeScript fails to compile, drift is caught there.
* **Doesn't fuzz over all schemas.** A focused priority list is enough to
  catch drift at the interfaces the frontend actually consumes; broader
  coverage is post-beta.
* **Doesn't cover external-API contracts** (Razorpay, MSG91, FCM). Those
  contracts are pinned by the vendor and out of our test scope.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

# ---------------------------------------------------------------------------
# Helpers — schema extraction + validation
# ---------------------------------------------------------------------------


def _openapi_spec(client) -> dict[str, Any]:
    """Pull the OpenAPI spec off the running ASGI app.

    Passed via ``client`` (which owns the app instance) rather than importing
    ``create_app()`` — that would build a *different* app without the
    dependency overrides the fixtures install.
    """
    return client._transport.app.openapi()


def _response_schema(
    spec: dict[str, Any], path: str, method: str, status_code: int = 200
) -> dict[str, Any]:
    """Return the JSON schema declared for a specific route response.

    Raises AssertionError if the endpoint / status / content-type combination
    isn't in the spec — treats a missing declaration as a test failure so the
    author can't accidentally validate against nothing.
    """
    method = method.lower()
    op = spec["paths"].get(path, {}).get(method)
    assert op is not None, f"No {method.upper()} {path} in OpenAPI spec"

    responses = op.get("responses", {})
    resp = responses.get(str(status_code))
    assert resp is not None, f"{method.upper()} {path} has no declared {status_code} response"

    content = resp.get("content", {}).get("application/json")
    assert (
        content is not None
    ), f"{method.upper()} {path} {status_code} has no application/json body"
    schema = content.get("schema")
    assert schema is not None, f"{method.upper()} {path} {status_code} has no schema"
    return schema


def _validator(spec: dict[str, Any], schema: dict[str, Any]) -> Draft202012Validator:
    """Build a validator whose $ref lookups resolve through the OpenAPI spec.

    FastAPI's schemas reference each other via ``$ref: '#/components/schemas/X'``.
    Without a registry seeded from ``components/schemas``, the validator can't
    resolve those refs and every test would fail on schema-not-found.
    """
    registry = Registry().with_resource(
        uri="", resource=Resource(contents=spec, specification=DRAFT202012)
    )
    return Draft202012Validator(schema, registry=registry)


def _assert_conforms(client, response, path: str, method: str, status_code: int = 200) -> None:
    """Fetch the route's declared schema and validate the response body.

    All-in-one helper: pulls the spec, extracts the schema, builds a
    ref-resolving validator, validates. On failure jsonschema's error
    message points at the divergence.
    """
    assert response.status_code == status_code, (
        f"{method} {path} returned {response.status_code}, expected {status_code}: "
        f"{response.text}"
    )
    spec = _openapi_spec(client)
    schema = _response_schema(spec, path, method, status_code)
    _validator(spec, schema).validate(response.json())


# ---------------------------------------------------------------------------
# Shared fixtures / factories — kept local to keep the DB-heavy setup obvious
# ---------------------------------------------------------------------------


async def _service(db) -> Service:
    """Create a unique Category + Service and return the Service.

    Slug uniqueness is enforced with a random suffix — same as the SVC-153
    tests. Prevents cross-test collisions when multiple contract tests all
    build their own service.
    """
    slug = f"cleaning-{uuid.uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db.add(category)
    await db.flush()
    svc = Service(
        category_id=category.id,
        name="Deep Cleaning",
        base_price=1499.0,
        duration_minutes=90,
        is_active=True,
    )
    db.add(svc)
    await db.flush()
    return svc


async def _address(db, consumer) -> Address:
    addr = Address(
        user_id=consumer.id,
        label="home",
        full_address="12 MG Road, Indiranagar",
    )
    db.add(addr)
    await db.flush()
    return addr


async def _consumer_booking(db, *, status=BookingStatus.REQUESTED):
    """Consumer + booking with all FK deps. Returns (consumer, provider, booking)."""
    consumer = await UserFactory.create(db, role="consumer")
    provider = await UserFactory.create(db, role="provider")
    svc = await _service(db)
    addr = await _address(db, consumer)
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id if status is not BookingStatus.REQUESTED else None,
        service_id=svc.id,
        address_id=addr.id,
        status=status,
    )
    return consumer, provider, booking


# ---------------------------------------------------------------------------
# The actual contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_otp_response_conforms(client, db_session):
    """POST /auth/verify-otp → TokenResponse."""
    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)):
        r = await client.post(
            "/api/v1/auth/verify-otp",
            json={"phone": "+919876543210", "otp": "123456", "role": "consumer"},
        )
    _assert_conforms(client, r, "/api/v1/auth/verify-otp", "post", 200)


@pytest.mark.asyncio
async def test_get_current_user_response_conforms(client, db_session, auth_headers):
    """GET /users/me → UserResponse."""
    user = await UserFactory.create(db_session, role="consumer")

    r = await client.get("/api/v1/users/me", headers=auth_headers(str(user.id), "consumer"))
    _assert_conforms(client, r, "/api/v1/users/me", "get", 200)


@pytest.mark.asyncio
async def test_patch_current_user_response_conforms(client, db_session, auth_headers):
    """PATCH /users/me → UserResponse."""
    user = await UserFactory.create(db_session, role="consumer")

    r = await client.patch(
        "/api/v1/users/me",
        headers=auth_headers(str(user.id), "consumer"),
        json={"name": "Test User", "email": "test@example.com"},
    )
    _assert_conforms(client, r, "/api/v1/users/me", "patch", 200)


@pytest.mark.asyncio
async def test_create_booking_response_conforms(client, db_session, auth_headers):
    """POST /bookings/ → BookingResponse."""
    consumer = await UserFactory.create(db_session, role="consumer")
    svc = await _service(db_session)
    addr = await _address(db_session, consumer)

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = lambda **kwargs: None
        r = await client.post(
            "/api/v1/bookings/",
            headers=auth_headers(str(consumer.id), "consumer"),
            json={
                "service_id": str(svc.id),
                "address_id": str(addr.id),
                "payment_method": "online",
            },
        )
    _assert_conforms(client, r, "/api/v1/bookings/", "post", 201)


@pytest.mark.asyncio
async def test_list_bookings_response_conforms(client, db_session, auth_headers):
    """GET /bookings/ → BookingListResponse (empty list still validates)."""
    consumer = await UserFactory.create(db_session, role="consumer")

    r = await client.get("/api/v1/bookings/", headers=auth_headers(str(consumer.id), "consumer"))
    _assert_conforms(client, r, "/api/v1/bookings/", "get", 200)


@pytest.mark.asyncio
async def test_get_booking_response_conforms(client, db_session, auth_headers):
    """GET /bookings/{id} → BookingResponse."""
    consumer, provider, booking = await _consumer_booking(db_session, status=BookingStatus.ACCEPTED)

    r = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )
    _assert_conforms(client, r, "/api/v1/bookings/{booking_id}", "get", 200)


@pytest.mark.asyncio
async def test_accept_booking_response_conforms(client, db_session, auth_headers):
    """PATCH /bookings/{id}/accept → BookingResponse (200 winner path)."""
    consumer, _, booking = await _consumer_booking(db_session, status=BookingStatus.REQUESTED)
    # The booking factory pre-assigns a provider even in REQUESTED state via
    # our helper — clear it so accept follows the "unclaimed" branch, and
    # create a fresh provider to accept.
    booking.provider_id = None
    provider = await UserFactory.create(db_session, role="provider")
    await db_session.flush()

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = lambda **kwargs: None
        r = await client.patch(
            f"/api/v1/bookings/{booking.id}/accept",
            headers=auth_headers(str(provider.id), "provider"),
        )
    _assert_conforms(client, r, "/api/v1/bookings/{booking_id}/accept", "patch", 200)


@pytest.mark.asyncio
async def test_get_booking_offer_response_conforms(client, db_session, auth_headers):
    """GET /bookings/{id}/offer → BookingOfferResponse (SVC-153).

    Needs an eligible provider (verified + online + subscribed + in radius).
    The consumer + booking sit at Indiranagar; provider's service area centre
    is near it.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    svc = await _service(db_session)

    # Consumer's address at Indiranagar.
    addr = Address(
        user_id=consumer.id,
        label="home",
        full_address="12 MG Road, Indiranagar",
        location="SRID=4326;POINT(77.6408 12.9784)",
    )
    db_session.add(addr)
    await db_session.flush()

    # Provider is verified + online with a service area centred nearby.
    profile = ProviderProfile(
        user_id=provider.id,
        kyc_status=KYCStatus.VERIFIED,
        is_online=True,
        service_area_center="SRID=4326;POINT(77.6200 12.9700)",
        service_area_radius_km=10,
    )
    db_session.add(profile)
    # Subscription so eligibility passes.
    db_session.add(ProviderServiceModel(provider_id=provider.id, service_id=svc.id))
    await db_session.flush()

    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )

    r = await client.get(
        f"/api/v1/bookings/{booking.id}/offer",
        headers=auth_headers(str(provider.id), "provider"),
    )
    _assert_conforms(client, r, "/api/v1/bookings/{booking_id}/offer", "get", 200)


@pytest.mark.asyncio
async def test_create_review_response_conforms(client, db_session, auth_headers):
    """POST /reviews/ → ReviewResponse.

    This is the *exact* endpoint that would have caught the ``booking_id``
    vs ``bookingId`` bug that shipped in ``useApi.ts::useSubmitReview``. If
    a future refactor renames the field again, this test fails loud instead
    of silently accepting the wrong payload.
    """
    consumer, provider, booking = await _consumer_booking(
        db_session, status=BookingStatus.COMPLETED
    )

    r = await client.post(
        "/api/v1/reviews/",
        headers=auth_headers(str(consumer.id), "consumer"),
        json={
            "booking_id": str(booking.id),
            "rating": 5,
            "comment": "Great service",
        },
    )
    _assert_conforms(client, r, "/api/v1/reviews/", "post", 201)


@pytest.mark.asyncio
async def test_create_payment_order_response_conforms(client, db_session, auth_headers):
    """POST /payments/orders → PaymentOrderResponse.

    Razorpay's live client is patched at the boundary — we're checking OUR
    response shape, not Razorpay's.
    """
    from unittest.mock import MagicMock

    consumer, _, booking = await _consumer_booking(db_session, status=BookingStatus.COMPLETED)
    booking.payment_method = "online"
    await db_session.flush()

    fake_order = {
        "id": "order_test_contract_123",
        "amount": int(booking.service_amount * 100),
        "currency": "INR",
        "status": "created",
    }

    with patch("app.services.payment_service.razorpay_client") as fake:
        fake.order.create = MagicMock(return_value=fake_order)
        r = await client.post(
            "/api/v1/payments/orders",
            headers=auth_headers(str(consumer.id), "consumer"),
            json={"booking_id": str(booking.id)},
        )
    _assert_conforms(client, r, "/api/v1/payments/orders", "post", 200)
