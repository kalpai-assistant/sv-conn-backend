"""
SVC-319 (team model, Phase A): ``provider_profiles.parent_provider_id`` and the
``payout_target_id`` rule.

A team member's earnings belong to their team owner; a solo provider or a team
owner is paid as themselves. No payout code reads the rule yet — payouts wait on
RazorpayX (Vipul, 2026-09-29) — so these tests pin the rule itself and that the
column survives a round trip through Postgres.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.provider_profile import ProviderProfile
from tests.factories.user_factory import UserFactory


def test_payout_target_is_the_provider_when_there_is_no_parent():
    user_id = uuid.uuid4()
    profile = ProviderProfile(user_id=user_id)

    assert profile.parent_provider_id is None
    assert profile.payout_target_id == user_id


def test_payout_target_is_the_parent_when_one_is_set():
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    profile = ProviderProfile(user_id=member_id, parent_provider_id=owner_id)

    assert profile.payout_target_id == owner_id


async def _provider(db) -> uuid.UUID:
    user = await UserFactory.create(db, role="provider")
    db.add(ProviderProfile(user_id=user.id))
    await db.flush()
    return user.id


async def _load(db, user_id: uuid.UUID) -> ProviderProfile:
    return (
        await db.execute(select(ProviderProfile).where(ProviderProfile.user_id == user_id))
    ).scalar_one()


@pytest.mark.asyncio
async def test_a_member_resolves_to_their_owner_after_a_round_trip(db_session):
    """The rule holds on rows read back from Postgres, not only on fresh objects.

    Ids are captured as plain values before ``expire_all``: touching an expired
    attribute under AsyncSession would lazy-load and raise ``MissingGreenlet``.
    """
    owner_id = await _provider(db_session)
    member_id = await _provider(db_session)

    member = await _load(db_session, member_id)
    member.parent_provider_id = owner_id
    await db_session.flush()
    db_session.expire_all()

    member = await _load(db_session, member_id)
    owner = await _load(db_session, owner_id)

    assert member.parent_provider_id == owner_id
    assert member.payout_target_id == owner_id
    # The owner is untouched: still solo-shaped, still paid as themselves.
    assert owner.parent_provider_id is None
    assert owner.payout_target_id == owner_id
