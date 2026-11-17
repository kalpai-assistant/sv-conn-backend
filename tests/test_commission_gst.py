"""
SVC-410: GST on the platform's commission, stored per booking and item, taken
off the SP's payout.

- ``commission.commission_gst`` is the one rule: the commission times the
  category's ``commission_gst_pct``, rounded half up to paise. ``take_home``
  (SVC-412's "You receive") uses it too.
- ``create_booking`` applies it to every item, with or without a chosen SP, at
  the item's own service category rate, so ``total_price = platform_commission
  + commission_gst + provider_payout`` per item and per booking.
- A cash booking's ledger debit is commission + GST.
- The booking response carries ``commission_gst``.
"""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.booking import Booking, PaymentMethod, PaymentStatus
from app.models.booking_item import BookingItem
from app.models.category import Category
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.services.commission import commission_gst, take_home
from app.tasks.payment_tasks import process_cash_payment
from app.utils.money import to_paise
from tests.test_accept_contract import _eligible_provider
from tests.test_price_lock import _consumer, _item, _post, _price, _service

D = Decimal


# --- commission_gst ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("commission", "pct", "gst"),
    [
        # The ticket's examples.
        ("600.24", "18", "108.04"),  # 108.0432
        ("750.00", "18.00", "135.00"),
        # Exactly half a paisa rounds up (banker's rounding would give 0.04 / 0.22).
        ("0.25", "18", "0.05"),  # 0.045
        ("1.25", "18", "0.23"),  # 0.225
        # Just under half a paisa rounds down.
        ("0.24", "18", "0.04"),  # 0.0432
        ("750.00", "5", "37.50"),
        ("750.00", "0", "0.00"),
        ("0", "18", "0.00"),
    ],
)
def test_commission_gst_is_the_rate_of_the_commission_rounded_half_up(commission, pct, gst):
    assert commission_gst(D(commission), D(pct)) == D(gst)


def test_commission_gst_takes_floats_and_ints_as_their_decimal_value():
    assert commission_gst(0.25, 18.0) == D("0.05")
    assert commission_gst(750, 18) == D("135.00")


def test_take_home_uses_commission_gst():
    variant = ServiceVariant(base_price=D("2499.00"), platform_commission=D("750.00"))
    assert take_home(variant, D("18.00"), D("2000.00")) == (
        D("600.24"),
        D("108.04"),
        D("1291.72"),
    )
    with patch("app.services.commission.commission_gst", return_value=D("1.00")) as gst:
        assert take_home(variant, D("18.00"), D("2000.00")) == (
            D("600.24"),
            D("1.00"),
            D("1398.76"),
        )
    gst.assert_called_once_with(D("600.24"), D("18.00"))


# --- create_booking ----------------------------------------------------------------


async def _set_gst_pct(db, service, pct):
    category = await db.get(Category, service.category_id)
    category.commission_gst_pct = D(pct)
    await db.flush()


async def _created(db, resp) -> tuple[Booking, list[BookingItem]]:
    assert resp.status_code == 201, resp.text
    booking = await db.get(Booking, uuid.UUID(resp.json()["id"]))
    items = list(
        (
            await db.execute(
                select(BookingItem)
                .where(BookingItem.booking_id == booking.id)
                .order_by(BookingItem.total_price)
            )
        ).scalars()
    )
    return booking, items


def _assert_adds_up(booking: Booking, items: list[BookingItem]) -> None:
    for item in items:
        assert item.platform_commission + item.commission_gst + item.provider_payout == (
            item.total_price
        )
    assert booking.platform_commission + booking.commission_gst + booking.provider_payout == (
        booking.service_amount
    )
    for field in ("total_price", "platform_commission", "commission_gst", "provider_payout"):
        total = sum((getattr(i, field) for i in items), start=D(0))
        booking_field = "service_amount" if field == "total_price" else field
        assert getattr(booking, booking_field) == total, field


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chosen_sp", "price", "commission", "gst", "payout"),
    [
        # The SVC-410 example: an SP at ₹2,000 on the ₹2,499 / ₹750 variant.
        (True, "2000.00", "600.24", "108.04", "1291.72"),
        # The platform's own price: ₹1,749 before SVC-410, ₹1,614 now.
        (False, "2499.00", "750.00", "135.00", "1614.00"),
    ],
)
async def test_gst_is_stored_and_taken_off_the_payout_matching_take_home(
    client, db_session, auth_headers, chosen_sp, price, commission, gst, payout
):
    service, variant = await _service(db_session)
    consumer, address = await _consumer(db_session)
    sp = None
    if chosen_sp:
        sp = await _eligible_provider(db_session, service)
        await _price(db_session, sp, service, variant, price)

    resp = await _post(
        client, auth_headers, consumer, address, [_item(service, variant, float(price))], sp=sp
    )

    booking, [item] = await _created(db_session, resp)
    expected = (D(price), D(commission), D(gst), D(payout))
    assert (
        item.total_price,
        item.platform_commission,
        item.commission_gst,
        item.provider_payout,
    ) == expected
    assert (
        booking.service_amount,
        booking.platform_commission,
        booking.commission_gst,
        booking.provider_payout,
    ) == expected
    # The SP's pricing screen promised the same amount (SVC-412).
    assert take_home(variant, D("18.00"), D(price)) == expected[1:]
    body = resp.json()
    assert (body["platform_commission"], body["commission_gst"], body["provider_payout"]) == (
        float(commission),
        float(gst),
        float(payout),
    )
    _assert_adds_up(booking, [item])


@pytest.mark.asyncio
async def test_the_services_own_category_rate_is_used(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    await _set_gst_pct(db_session, service, "5.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(client, auth_headers, consumer, address, [_item(service, variant, 2499)])

    booking, [item] = await _created(db_session, resp)
    # 750 x 5% = 37.50.
    assert (item.commission_gst, item.provider_payout) == (D("37.50"), D("1711.50"))
    assert (booking.commission_gst, booking.provider_payout) == (D("37.50"), D("1711.50"))


@pytest.mark.asyncio
async def test_a_cart_across_two_categories_sums_each_items_gst(client, db_session, auth_headers):
    service_a, variant_a = await _service(db_session)
    service_b, variant_b = await _service(
        db_session, base_price=D("1000.00"), commission=D("300.00")
    )
    await _set_gst_pct(db_session, service_b, "5.00")
    assert service_a.category_id != service_b.category_id
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client,
        auth_headers,
        consumer,
        address,
        [_item(service_a, variant_a, 2499), _item(service_b, variant_b, 1000, quantity=3)],
    )

    booking, items = await _created(db_session, resp)
    by_service = {i.service_id: i for i in items}
    a, b = by_service[service_a.id], by_service[service_b.id]
    # A: 750 x 18% = 135.00. B: 3 x 300 = 900 commission, 900 x 5% = 45.00.
    assert (a.platform_commission, a.commission_gst, a.provider_payout) == (
        D("750.00"),
        D("135.00"),
        D("1614.00"),
    )
    assert (b.platform_commission, b.commission_gst, b.provider_payout) == (
        D("900.00"),
        D("45.00"),
        D("2055.00"),
    )
    assert (
        booking.service_amount,
        booking.platform_commission,
        booking.commission_gst,
        booking.provider_payout,
    ) == (D("5499.00"), D("1650.00"), D("180.00"), D("3669.00"))
    _assert_adds_up(booking, items)


@pytest.mark.asyncio
async def test_a_chosen_sp_cart_across_two_categories_adds_up(client, db_session, auth_headers):
    service_a, variant_a = await _service(db_session)
    service_b, variant_b = await _service(
        db_session, base_price=D("1000.00"), commission=D("300.00")
    )
    await _set_gst_pct(db_session, service_b, "12.00")
    sp = await _eligible_provider(db_session, service_a)
    db_session.add(ProviderServiceModel(provider_id=sp.id, service_id=service_b.id))
    await _price(db_session, sp, service_a, variant_a, "2000.00")
    await _price(db_session, sp, service_b, variant_b, "799.99")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client,
        auth_headers,
        consumer,
        address,
        [_item(service_a, variant_a, 2000), _item(service_b, variant_b, 799.99, quantity=2)],
        sp=sp,
    )

    booking, items = await _created(db_session, resp)
    b = next(i for i in items if i.service_id == service_b.id)
    # 1599.98 x 300/1000 = 479.994 -> 479.99; x 12% = 57.5988 -> 57.60.
    assert (b.platform_commission, b.commission_gst, b.provider_payout) == (
        D("479.99"),
        D("57.60"),
        D("1062.39"),
    )
    assert booking.commission_gst == D("108.04") + D("57.60")
    _assert_adds_up(booking, items)


@pytest.mark.asyncio
async def test_quantity_works_on_the_item_total(client, db_session, auth_headers):
    """As SVC-411 does for the commission: GST on the item total's commission,
    so the payout may differ by a paisa from quantity × take_home."""
    service, variant = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    await _price(db_session, sp, service, variant, "2000.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client, auth_headers, consumer, address, [_item(service, variant, 2000, 2)], sp=sp
    )

    booking, [item] = await _created(db_session, resp)
    # 4000 x 750/2499 = 1200.48; x 18% = 216.0864 -> 216.09.
    assert (item.platform_commission, item.commission_gst, item.provider_payout) == (
        D("1200.48"),
        D("216.09"),
        D("2583.43"),
    )
    _, _, one = take_home(variant, D("18.00"), D("2000.00"))
    assert 2 * one - item.provider_payout == D("0.01")
    _assert_adds_up(booking, [item])


@pytest.mark.asyncio
async def test_a_service_with_no_variant_pays_gst_on_the_legacy_commission(
    client, db_session, auth_headers
):
    """The pre-variant fallback (PLATFORM_COMMISSION_PERCENT of services.base_price)."""
    consumer, address = await _consumer(db_session)
    slug = f"gst-{uuid.uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()
    service = Service(
        category_id=category.id,
        name="Basic Pipe Fix",
        base_price=D("100.00"),
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)
    await db_session.flush()

    resp = await client.post(
        "/api/v1/bookings/",
        json={
            "items": [{"service_id": str(service.id), "quantity": 1}],
            "address_id": str(address.id),
            "payment_method": "online",
        },
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    booking, [item] = await _created(db_session, resp)
    assert item.service_variant_id is None
    commission = D("100.00") * get_settings().PLATFORM_COMMISSION_PERCENT / 100
    assert item.platform_commission == commission
    assert item.commission_gst == commission_gst(commission, D("18.00"))
    _assert_adds_up(booking, [item])


# --- Cash ledger debit -------------------------------------------------------------


def _run_cash_task(booking: Booking) -> MagicMock:
    """Run ``process_cash_payment`` on ``booking`` and return the patched debit.

    The task opens its own sync session, which can't see the async test
    transaction, so the session is a mock that hands back ``booking``.
    """
    db = MagicMock()
    db.get.return_value = booking
    session_factory = MagicMock()
    session_factory.return_value.__enter__.return_value = db
    with (
        patch("app.database.SyncSessionLocal", session_factory),
        patch("app.services.ledger_service.sync_debit_cash_commission") as debit,
    ):
        process_cash_payment.run(str(booking.id))
    db.commit.assert_called_once()
    assert booking.payment_status == PaymentStatus.PAID
    return debit


def _cash_booking(**money) -> Booking:
    return Booking(
        id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        payment_status=PaymentStatus.PENDING,
        payment_method=PaymentMethod.CASH,
        **money,
    )


def test_the_cash_debit_is_the_commission_plus_its_gst():
    booking = _cash_booking(
        service_amount=D("2499.00"),
        platform_commission=D("750.00"),
        commission_gst=D("135.00"),
        provider_payout=D("1614.00"),
    )

    debit = _run_cash_task(booking)

    debit.assert_called_once()
    assert debit.call_args.kwargs["amount_paise"] == 88500
    assert debit.call_args.kwargs["provider_id"] == booking.provider_id
    # What the SP collected, less what is theirs.
    assert debit.call_args.kwargs["amount_paise"] == to_paise(
        booking.service_amount - booking.provider_payout
    )


def test_a_cash_booking_from_before_svc_410_debits_only_the_commission():
    booking = _cash_booking(
        service_amount=D("100.00"),
        platform_commission=D("20.00"),
        commission_gst=D("0.00"),
        provider_payout=D("80.00"),
    )

    debit = _run_cash_task(booking)

    assert debit.call_args.kwargs["amount_paise"] == 2000


def test_a_cash_booking_with_no_commission_is_not_debited():
    booking = _cash_booking(
        service_amount=D("100.00"),
        platform_commission=D("0.00"),
        commission_gst=D("0.00"),
        provider_payout=D("100.00"),
    )

    debit = _run_cash_task(booking)

    debit.assert_not_called()
