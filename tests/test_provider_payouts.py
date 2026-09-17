"""
Provider-facing payout endpoints (SVC-303).

Covers GET /providers/me/payouts/summary and GET /providers/me/payouts.

The cases worth having are the two that make this endpoint necessary at all.
A provider's earnings and their next bank transfer are different numbers, and
the naive implementation — sum the bookings matching ``_owed_filter`` —
**overstates** the transfer:

* **cash commission** is netted off. On a cash job the provider collected the
  full amount at the door, so the platform's 20% comes out of their next
  online transfer.
* **a T+1 settlement gate** holds a completed job out of the payable balance
  until Razorpay has actually deposited that money with us.

A provider told "₹3,480 coming" who receives ₹2,900 has been misled by their
own app, which is the bug this whole ticket exists to close. Both behaviours
are pinned below.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.address import Address
from app.models.booking import (
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
    SettlementStatus,
)
from app.models.category import Category
from app.models.provider_ledger_entry import (
    LedgerEntryType,
    LedgerReferenceType,
    ProviderLedgerEntry,
)
from app.models.provider_profile import KYCStatus, PayoutMethod, ProviderProfile
from app.models.service import Service
from app.models.settlement_statement import SettlementStatement
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

SUMMARY_URL = "/api/v1/providers/me/payouts/summary"
LIST_URL = "/api/v1/providers/me/payouts"


async def _seed_service(db) -> Service:
    suffix = uuid.uuid4().hex[:8]
    category = Category(
        name=f"Repair {suffix}", slug=f"repair-{suffix}", is_active=True, sort_order=1
    )
    db.add(category)
    await db.flush()
    service = Service(
        category_id=category.id,
        name=f"AC Service {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db.add(service)
    await db.flush()
    return service


async def _seed_provider(db, *, payout_method: PayoutMethod | None = PayoutMethod.UPI):
    user = await UserFactory.create(db, role="provider")
    db.add(
        ProviderProfile(
            user_id=user.id,
            kyc_status=KYCStatus.VERIFIED,
            payout_method=payout_method,
        )
    )
    await db.flush()
    return user


async def _seed_booking(
    db,
    provider,
    consumer,
    *,
    payout: float = 100.0,
    payment_method: PaymentMethod = PaymentMethod.ONLINE,
    settlement_status: SettlementStatus = SettlementStatus.PENDING,
    settled: bool = True,
):
    """A completed booking, plus the wallet credit its completion would write.

    ``settled`` mirrors ``bookings.settled_at`` — whether Razorpay has paid the
    platform for it yet. The T+1 gate in ``wallet_balance_paise`` reads exactly
    that, so this is the switch those tests flip.
    """
    service = await _seed_service(db)
    address = Address(user_id=consumer.id, label="home", full_address="x")
    db.add(address)
    await db.flush()

    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        payment_method=payment_method,
        provider_payout=payout,
    )
    # Not a factory parameter — set on the model, which defaults it to PENDING.
    booking.settlement_status = settlement_status
    if settled and payment_method is not PaymentMethod.CASH:
        booking.settled_at = datetime.now(UTC)
    await db.flush()

    # Online completion credits the wallet; cash does not (the provider was
    # paid at the door) — mirroring BookingService/ledger_service.
    if payment_method is not PaymentMethod.CASH:
        db.add(
            ProviderLedgerEntry(
                provider_id=provider.id,
                amount_paise=int(round(payout * 100)),
                entry_type=LedgerEntryType.BOOKING_COMPLETED,
                reference_type=LedgerReferenceType.BOOKING,
                reference_id=booking.id,
            )
        )
        await db.flush()
    return booking


async def _add_cash_commission(db, provider, *, amount: float, booking_id=None):
    """The debit ``process_cash_payment`` writes for a completed cash job."""
    db.add(
        ProviderLedgerEntry(
            provider_id=provider.id,
            amount_paise=-int(round(amount * 100)),
            entry_type=LedgerEntryType.CASH_COMMISSION,
            reference_type=LedgerReferenceType.BOOKING,
            reference_id=booking_id,
        )
    )
    await db.flush()


async def _add_statement(db, provider, *, net: float, deducted: float = 0.0, days_ago: int = 0):
    statement = SettlementStatement(
        provider_id=provider.id,
        payout_audit_id=None,
        gross_credit_paise=int(round((net + deducted) * 100)),
        cash_commission_deducted_paise=int(round(deducted * 100)),
        other_adjustments_paise=0,
        net_paid_paise=int(round(net * 100)),
        platform_balance_owed_paise=0,
    )
    db.add(statement)
    await db.flush()
    if days_ago:
        statement.created_at = datetime.now(UTC) - timedelta(days=days_ago)
        await db.flush()
    return statement


# ---------------------------------------------------------------------------
# The netting — why this endpoint exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cash_commission_is_netted_off_the_transfer(client, auth_headers, db_session):
    """The headline figure is what the bank will send, not what the jobs are worth.

    ₹500 of online work with ₹100 of cash commission owed transfers ₹400. A
    provider shown ₹500 here and paid ₹400 has been misled by their own app.
    """
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(db_session, provider, consumer, payout=500.0)
    await _add_cash_commission(db_session, provider, amount=100.0)

    body = (
        await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))
    ).json()

    assert body["pending_amount"] == 400.0
    # The gross is still reported, so the screen can name the difference
    # instead of leaving the provider to discover it at the bank.
    assert body["gross_amount"] == 500.0


@pytest.mark.asyncio
async def test_unsettled_job_is_owed_but_not_yet_payable(client, auth_headers, db_session):
    """The T+1 gate: completed is not the same as payable.

    A credit only counts once Razorpay has deposited that money with us
    (``bookings.settled_at``). Paying sooner would come from platform float,
    which the RBI PA rules forbid — so a job finished an hour ago is genuinely
    owed and genuinely not yet transferable.
    """
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(db_session, provider, consumer, payout=300.0, settled=True)
    await _seed_booking(db_session, provider, consumer, payout=200.0, settled=False)

    body = (
        await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))
    ).json()

    assert body["pending_amount"] == 300.0
    # Both jobs are queued for payout — the count is about jobs, the amount
    # about money, and they legitimately disagree here.
    assert body["gross_amount"] == 500.0
    assert body["pending_booking_count"] == 2


@pytest.mark.asyncio
async def test_owing_the_platform_reports_balance_owed_not_negative_pending(
    client, auth_headers, db_session
):
    """A run of cash work can leave the provider owing us.

    ``create_provider_payout`` pays nothing in that case and writes a statement
    instead. The figure must not go negative on screen: pending clamps to zero
    and the debt is reported in its own field, so the UI can explain it plainly
    rather than rendering "-₹200 coming to you".
    """
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(db_session, provider, consumer, payout=100.0)
    await _add_cash_commission(db_session, provider, amount=300.0)

    body = (
        await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))
    ).json()

    assert body["pending_amount"] == 0.0
    assert body["balance_owed"] == 200.0


@pytest.mark.asyncio
async def test_cash_bookings_never_enter_the_payable_total(client, auth_headers, db_session):
    """A cash job is earned, and owed to nobody — the provider already holds it."""
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(
        db_session, provider, consumer, payout=400.0, payment_method=PaymentMethod.CASH
    )

    body = (
        await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))
    ).json()

    assert body["pending_amount"] == 0.0
    assert body["gross_amount"] == 0.0


@pytest.mark.asyncio
async def test_already_settled_jobs_drop_out_of_pending(client, auth_headers, db_session):
    """Only PENDING settlements are queued; a paid job is not coming again."""
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(db_session, provider, consumer, payout=250.0)
    await _seed_booking(
        db_session,
        provider,
        consumer,
        payout=999.0,
        settlement_status=SettlementStatus.COMPLETED,
    )

    body = (
        await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))
    ).json()

    assert body["gross_amount"] == 250.0
    assert body["pending_booking_count"] == 1


# ---------------------------------------------------------------------------
# Last paid, and the never-paid case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_paid_provider_gets_nulls_not_zeros(client, auth_headers, db_session):
    """Null so the UI can say "no payouts yet" rather than "₹0 on —"."""
    provider = await _seed_provider(db_session)

    body = (
        await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))
    ).json()

    assert body["last_paid_at"] is None
    assert body["last_paid_amount"] is None


@pytest.mark.asyncio
async def test_last_paid_reports_the_newest_statement(client, auth_headers, db_session):
    provider = await _seed_provider(db_session)

    await _add_statement(db_session, provider, net=1000.0, days_ago=30)
    await _add_statement(db_session, provider, net=2500.0, days_ago=2)

    body = (
        await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))
    ).json()

    assert body["last_paid_amount"] == 2500.0
    assert body["last_paid_at"] is not None


@pytest.mark.asyncio
async def test_a_cycle_that_paid_nothing_is_not_a_payout(client, auth_headers, db_session):
    """A statement exists for every cycle, including ones that moved no money.

    Those are not payouts, and showing one as "last paid" would tell a provider
    money arrived when none did.
    """
    provider = await _seed_provider(db_session)

    await _add_statement(db_session, provider, net=800.0, days_ago=10)
    await _add_statement(db_session, provider, net=0.0, days_ago=1)

    body = (
        await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))
    ).json()

    assert body["last_paid_amount"] == 800.0


@pytest.mark.asyncio
async def test_payout_set_reflects_whether_money_can_be_sent(client, auth_headers, db_session):
    """The one field that turns "₹400 coming" into "₹400 is stuck, fix it"."""
    with_method = await _seed_provider(db_session)
    without = await _seed_provider(db_session, payout_method=None)

    assert (
        await client.get(SUMMARY_URL, headers=auth_headers(str(with_method.id), "provider"))
    ).json()["payout_set"] is True
    assert (
        await client.get(SUMMARY_URL, headers=auth_headers(str(without.id), "provider"))
    ).json()["payout_set"] is False


# ---------------------------------------------------------------------------
# The statement list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_statements_newest_first(client, auth_headers, db_session):
    provider = await _seed_provider(db_session)

    await _add_statement(db_session, provider, net=500.0, days_ago=20)
    await _add_statement(db_session, provider, net=900.0, days_ago=3)

    body = (await client.get(LIST_URL, headers=auth_headers(str(provider.id), "provider"))).json()

    assert body["total"] == 2
    assert [e["amount"] for e in body["entries"]] == [900.0, 500.0]


@pytest.mark.asyncio
async def test_list_surfaces_what_was_deducted_from_each_payout(client, auth_headers, db_session):
    """The difference between what the provider expected and what arrived.

    A smaller transfer with no explanation is the complaint this field exists
    to prevent.
    """
    provider = await _seed_provider(db_session)
    await _add_statement(db_session, provider, net=900.0, deducted=140.0, days_ago=1)

    body = (await client.get(LIST_URL, headers=auth_headers(str(provider.id), "provider"))).json()

    assert body["entries"][0]["cash_commission_deducted"] == 140.0


@pytest.mark.asyncio
async def test_list_omits_cycles_that_moved_no_money(client, auth_headers, db_session):
    """Keeps this a list of money received, not a ledger of attempts."""
    provider = await _seed_provider(db_session)

    await _add_statement(db_session, provider, net=700.0, days_ago=5)
    await _add_statement(db_session, provider, net=0.0, days_ago=1)

    body = (await client.get(LIST_URL, headers=auth_headers(str(provider.id), "provider"))).json()

    assert body["total"] == 1
    assert body["entries"][0]["amount"] == 700.0


@pytest.mark.asyncio
async def test_list_exposes_no_internal_identifiers(client, auth_headers, db_session):
    """AC9: a provider learns what happened to their money, not who moved it.

    No admin id, no RazorpayX payout id, no reference id, no error text — those
    answer "who authorised this and through which gateway", which is the
    platform's business.
    """
    provider = await _seed_provider(db_session)
    await _add_statement(db_session, provider, net=500.0, days_ago=1)

    body = (await client.get(LIST_URL, headers=auth_headers(str(provider.id), "provider"))).json()

    assert set(body["entries"][0]) == {
        "id",
        "paid_at",
        "amount",
        "cash_commission_deducted",
    }


@pytest.mark.asyncio
async def test_a_provider_sees_only_their_own_payouts(client, auth_headers, db_session):
    mine = await _seed_provider(db_session)
    theirs = await _seed_provider(db_session)

    await _add_statement(db_session, mine, net=100.0, days_ago=1)
    await _add_statement(db_session, theirs, net=9999.0, days_ago=1)

    body = (await client.get(LIST_URL, headers=auth_headers(str(mine.id), "provider"))).json()

    assert body["total"] == 1
    assert body["entries"][0]["amount"] == 100.0


@pytest.mark.asyncio
async def test_both_endpoints_are_provider_only(client, auth_headers, db_session):
    consumer = await UserFactory.create(db_session, role="consumer")
    headers = auth_headers(str(consumer.id), "consumer")

    assert (await client.get(SUMMARY_URL, headers=headers)).status_code == 403
    assert (await client.get(LIST_URL, headers=headers)).status_code == 403


# ---------------------------------------------------------------------------
# Per-job settlement state on the earnings rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_earnings_rows_carry_their_settlement_state(client, auth_headers, db_session):
    """So the screen can explain, per job, why the earned total and the
    transfer differ — rather than leaving the provider to infer it."""
    from app.models.booking_status_log import BookingStatusLog

    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    booking = await _seed_booking(
        db_session,
        provider,
        consumer,
        payout=250.0,
        payment_method=PaymentMethod.CASH,
    )
    db_session.add(
        BookingStatusLog(
            booking_id=booking.id,
            from_status=BookingStatus.IN_PROGRESS.value,
            to_status=BookingStatus.COMPLETED.value,
            changed_by=provider.id,
        )
    )
    await db_session.flush()

    body = (
        await client.get(
            "/api/v1/providers/me/earnings?range=month",
            headers=auth_headers(str(provider.id), "provider"),
        )
    ).json()

    row = body["bookings"][0]
    # Cash is surfaced as payment_method, not as a settlement state: such a
    # booking never enters the payout path, and labelling it "paid" would imply
    # a transfer that never happened.
    assert row["payment_method"] == PaymentMethod.CASH.value
    assert row["settlement_status"] == SettlementStatus.PENDING.value
