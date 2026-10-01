"""
SVC-339 (team model, corrective Phase A): the ``professionals`` table at the ORM
level.

The migration and its backfill are covered in ``test_migrations.py``. These pin
what code will lean on: a Pro hangs off exactly one SP, the solo case is two rows
sharing a ``user_id``, and a booking's payee resolves through
``booking.professional.service_provider_id`` — under AsyncSession with eager
loading, and in a sync context the way the Celery worker would read it.
"""

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models.address import Address
from app.models.booking import Booking
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _solo_provider(db) -> tuple[uuid.UUID, Professional]:
    """An SP with its Pro-for-self, as the migration creates for every provider."""
    user = await UserFactory.create(db, role="provider")
    db.add(ProviderProfile(user_id=user.id))
    await db.flush()
    pro = Professional(service_provider_id=user.id, user_id=user.id, display_name="Sunita")
    db.add(pro)
    await db.flush()
    return user.id, pro


async def _booking(db, provider_id: uuid.UUID) -> Booking:
    consumer = await UserFactory.create(db, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="1 Test St")
    db.add(address)
    await db.flush()
    return await BookingFactory.create(
        db, consumer_id=consumer.id, provider_id=provider_id, address_id=address.id
    )


@pytest.mark.asyncio
async def test_a_new_pro_gets_the_schema_defaults(db_session):
    _, pro = await _solo_provider(db_session)
    await db_session.refresh(pro)

    assert pro.kyc_status == KYCStatus.PENDING
    assert pro.is_online is False
    assert pro.last_location is None
    assert pro.created_at is not None


@pytest.mark.asyncio
async def test_the_solo_case_is_an_sp_and_a_pro_sharing_one_user_id(db_session):
    sp_id, _ = await _solo_provider(db_session)
    db_session.expire_all()

    sp = (
        await db_session.execute(
            select(ProviderProfile)
            .where(ProviderProfile.user_id == sp_id)
            .options(selectinload(ProviderProfile.professionals))
        )
    ).scalar_one()

    assert len(sp.professionals) == 1
    assert sp.professionals[0].user_id == sp.user_id == sp_id
    assert sp.professionals[0].service_provider_id == sp_id


@pytest.mark.asyncio
async def test_a_user_cannot_be_the_same_sps_pro_twice(db_session):
    sp_id, _ = await _solo_provider(db_session)

    db_session.add(Professional(service_provider_id=sp_id, user_id=sp_id, display_name="Again"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_payout_resolves_through_the_professional_to_the_sp(db_session):
    """``booking.professional.service_provider_id`` is the payee — the rule that
    replaces SVC-319's ``payout_target_id``. For a solo booking it is the same
    person as ``booking.provider_id``."""
    sp_id, pro = await _solo_provider(db_session)
    booking = await _booking(db_session, sp_id)
    booking.professional_id = pro.id
    await db_session.flush()
    booking_id = booking.id
    db_session.expire_all()

    loaded = (
        await db_session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.professional).selectinload(Professional.service_provider))
        )
    ).scalar_one()

    assert loaded.professional.service_provider_id == sp_id
    assert loaded.professional.service_provider.user_id == sp_id
    assert loaded.provider_id == sp_id


@pytest.mark.asyncio
async def test_payout_resolution_lazy_loads_in_a_sync_context(db_session):
    """The Celery worker reads through ``SyncSessionLocal``, where relationships
    lazy-load instead of raising ``MissingGreenlet``. ``run_sync`` gives the same
    sync context on the test's session."""
    sp_id, pro = await _solo_provider(db_session)
    booking = await _booking(db_session, sp_id)
    booking.professional_id = pro.id
    await db_session.flush()
    booking_id = booking.id
    db_session.expire_all()

    def payee(sync_session) -> uuid.UUID:
        b = sync_session.get(Booking, booking_id)
        return b.professional.service_provider.user_id

    assert await db_session.run_sync(payee) == sp_id


@pytest.mark.asyncio
async def test_deleting_an_sp_deletes_its_pros(db_session):
    sp_id, pro = await _solo_provider(db_session)
    pro_id = pro.id

    await db_session.execute(delete(ProviderProfile).where(ProviderProfile.user_id == sp_id))
    db_session.expire_all()

    assert await db_session.get(Professional, pro_id) is None
