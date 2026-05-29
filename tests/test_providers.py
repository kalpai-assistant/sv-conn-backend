"""
Provider service-subscription integration tests (SVC-23).
Covers POST /providers/me/services and DELETE /providers/me/services/{id}.
"""
import pytest
from uuid import uuid4
from sqlalchemy import select

from app.models.category import Category
from app.models.provider_profile import ProviderProfile, KYCStatus
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from tests.factories.user_factory import UserFactory


async def _seed_service(db) -> Service:
    """Insert an active Category + Service and return the Service."""
    category = Category(
        name="Plumbing",
        slug=f"plumbing-{uuid4()}",
        is_active=True,
        sort_order=1,
    )
    db.add(category)
    await db.flush()

    service = Service(
        category_id=category.id,
        name="Basic Pipe Fix",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db.add(service)
    await db.flush()
    return service


async def _seed_provider(db, kyc_status: KYCStatus = KYCStatus.VERIFIED):
    """Create a provider User and matching ProviderProfile with given KYC status."""
    user = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(user_id=user.id, kyc_status=kyc_status)
    db.add(profile)
    await db.flush()
    return user, profile


@pytest.mark.asyncio
async def test_subscribe_success(client, auth_headers, db_session):
    """Verified provider subscribes → 201, row appears in provider_services."""
    user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.VERIFIED)
    service = await _seed_service(db_session)

    response = await client.post(
        "/api/v1/providers/me/services",
        json={"service_id": str(service.id)},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 201
    data = response.json()
    assert data["provider_id"] == str(user.id)
    assert data["service_id"] == str(service.id)

    result = await db_session.execute(
        select(ProviderServiceModel).where(
            ProviderServiceModel.provider_id == user.id,
            ProviderServiceModel.service_id == service.id,
        )
    )
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_subscribe_unverified_kyc(client, auth_headers, db_session):
    """Provider with PENDING KYC → 403."""
    user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.PENDING)
    service = await _seed_service(db_session)

    response = await client.post(
        "/api/v1/providers/me/services",
        json={"service_id": str(service.id)},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 403
    assert "verified" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_subscribe_no_profile(client, auth_headers, db_session):
    """Provider user without a ProviderProfile row → 403 with 'profile not found' message."""
    user = await UserFactory.create(db_session, role="provider")
    service = await _seed_service(db_session)

    response = await client.post(
        "/api/v1/providers/me/services",
        json={"service_id": str(service.id)},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 403
    assert "profile not found" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_subscribe_duplicate(client, auth_headers, db_session):
    """Already-subscribed provider → 409."""
    user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.VERIFIED)
    service = await _seed_service(db_session)
    db_session.add(ProviderServiceModel(provider_id=user.id, service_id=service.id))
    await db_session.flush()

    response = await client.post(
        "/api/v1/providers/me/services",
        json={"service_id": str(service.id)},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_subscribe_service_not_found(client, auth_headers, db_session):
    """Non-existent service_id → 404. (Coverage gap not in ticket.)"""
    user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.VERIFIED)

    response = await client.post(
        "/api/v1/providers/me/services",
        json={"service_id": str(uuid4())},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unsubscribe_success(client, auth_headers, db_session):
    """Existing subscription → DELETE returns 204 and row is gone."""
    user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.VERIFIED)
    service = await _seed_service(db_session)
    db_session.add(ProviderServiceModel(provider_id=user.id, service_id=service.id))
    await db_session.flush()

    response = await client.delete(
        f"/api/v1/providers/me/services/{service.id}",
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 204
    assert response.content == b""

    result = await db_session.execute(
        select(ProviderServiceModel).where(
            ProviderServiceModel.provider_id == user.id,
            ProviderServiceModel.service_id == service.id,
        )
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_unsubscribe_not_found(client, auth_headers, db_session):
    """Unsubscribing from a service the provider never subscribed to → 404."""
    user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.VERIFIED)
    service = await _seed_service(db_session)

    response = await client.delete(
        f"/api/v1/providers/me/services/{service.id}",
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 404
