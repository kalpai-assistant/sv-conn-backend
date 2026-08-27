"""A booking's transition history reaches the client (SVC-248).

``booking_status_logs`` was write-only. Every transition wrote a row and
nothing ever read one back, so ``BookingResponse`` carried only the *current*
status and the tracking timeline could not say when a booking reached any
stage — nor, for a booking that ended early, how far it actually got. A
booking cancelled while EN_ROUTE was indistinguishable from one cancelled
seconds after being requested.

Four things are pinned here:

* **What is exposed, and what is not.** ``changed_by`` and ``metadata_`` stay
  server-side. The first names another person; the second is free-form JSONB
  whose historical contents nobody has audited.
* **Order.** Oldest first, because that is what a timeline means.
* **No N+1.** The list route serves up to 50 rows and the admin listing up to
  200. ``test_list_route_loads_history_in_one_query`` counts statements; every
  other assertion in this file passes just as happily against a per-row load.
* **Bookings with no history at all.** The audit table arrived partway through
  the project, so early rows have no entries. They must serialise as an empty
  list rather than fault.

The hydration rule this depends on — ``status_logs`` is ``raise_on_sql``, so
every path must call ``attach_status_logs`` — is the same one
``test_booking_address.py`` pins for the address.
"""

import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.booking_status_log import BookingStatusLog
from app.models.category import Category
from app.models.service import Service
from app.schemas.booking import BookingResponse
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _booking(db_session, *, status: BookingStatus = BookingStatus.ACCEPTED):
    """One booking with its two parties, a real service row, and a real address.

    The address is not incidental: ``bookings.consumer_address_id`` is a real
    foreign key, and the factory invents a uuid when none is passed.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    cat = Category(name="Cleaning", slug="cleaning", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name="Basic Clean",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    address = Address(
        user_id=consumer.id,
        label="home",
        full_address="42 Residency Road, Bengaluru 560025",
    )
    db_session.add_all([svc, address])
    await db_session.flush()

    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=address.id,
        status=status,
    )
    return consumer, provider, booking, address


async def _log(db_session, booking_id, to_status, *, changed_by, metadata=None):
    """Write one transition row directly, the way ``_log_transition`` does."""
    row = BookingStatusLog(
        booking_id=booking_id,
        from_status="none",
        to_status=to_status,
        changed_by=changed_by,
        metadata_=metadata or {},
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ------------------------------------------------------------------
# What comes back
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_returns_history_oldest_first(client, auth_headers, db_session):
    """The timeline reads top to bottom, so the API answers in that order."""
    consumer, _, booking, _addr = await _booking(db_session)
    for status in ("requested", "accepted", "en_route"):
        await _log(db_session, booking.id, status, changed_by=consumer.id)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    history = response.json()["status_history"]
    assert [entry["to_status"] for entry in history] == ["requested", "accepted", "en_route"]


@pytest.mark.asyncio
async def test_history_withholds_changed_by_and_metadata(client, auth_headers, db_session):
    """The audit columns stay server-side.

    ``changed_by`` is usually the assigned provider's user id — who moved a
    booking is an operational fact, not something the consumer's app should be
    told. ``metadata_`` is free-form JSONB written by whatever called
    ``_log_transition``; its historical contents are unaudited, and a field
    whose contents are unknown cannot be knowingly published.

    Asserted on the raw keys rather than on the schema, so that widening
    ``BookingStatusLogResponse`` later has to come past this test.

    ``changed_by`` is a third user, not the booking's own provider: the
    provider's id appears in the response legitimately as ``provider_id``, so
    an absence check against it could never fail and would prove nothing.
    """
    consumer, _provider, booking, _addr = await _booking(db_session)
    admin = await UserFactory.create(db_session, role="admin")
    await _log(
        db_session,
        booking.id,
        "cancelled",
        changed_by=admin.id,
        metadata={"reason": "internal note nobody audited"},
    )

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    entry = response.json()["status_history"][0]
    assert set(entry) == {"to_status", "created_at"}
    assert str(admin.id) not in response.text
    assert "internal note nobody audited" not in response.text


@pytest.mark.asyncio
async def test_booking_with_no_logs_serialises_as_empty(client, auth_headers, db_session):
    """Rows predating the audit trail render, rather than fault.

    ``booking_status_logs`` was added partway through the project, so the
    oldest bookings have no entries at all. Empty is a real answer here and has
    to survive serialisation — which is also why the field is a required empty
    list rather than nullable.
    """
    consumer, _, booking, _addr = await _booking(db_session)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status_history"] == []


@pytest.mark.asyncio
async def test_status_history_is_required_in_the_schema():
    """Non-defaulted, so generated clients type it as always-present.

    A field with a default falls out of OpenAPI ``required`` and codegen types
    it optional — the trap ``BookingResponse.address`` carries a comment about.
    An empty list is a real answer and must be distinguishable from a route
    that does not send the field at all.
    """
    assert "status_history" in BookingResponse.model_json_schema()["required"]


# ------------------------------------------------------------------
# Hydration
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_route_loads_history_in_one_query(client, auth_headers, db_session):
    """Three bookings, one history query — not one per row.

    The consumer list serves up to 50 rows and the admin listing up to 200, so
    a per-row load is N+1 at the exact sizes that matter. Counting statements
    is the only assertion that catches it: every other test in this file passes
    against a per-row fetch.
    """
    consumer, provider, first, address = await _booking(db_session)
    await _log(db_session, first.id, "requested", changed_by=consumer.id)
    for _ in range(2):
        extra = await BookingFactory.create(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            service_id=first.items[0].service_id,
            address_id=address.id,
            status=BookingStatus.ACCEPTED,
        )
        await _log(db_session, extra.id, "requested", changed_by=consumer.id)

    executed: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        response = await client.get(
            "/api/v1/bookings/",
            headers=auth_headers(str(consumer.id), "consumer"),
        )
    finally:
        event.remove(Engine, "before_cursor_execute", _record)

    assert response.status_code == 200, response.text
    assert len(response.json()["bookings"]) == 3

    history_queries = [s for s in executed if "FROM booking_status_logs" in s]
    assert len(history_queries) == 1, (
        f"expected one booking_status_logs query for the page, got {len(history_queries)}:\n"
        + "\n".join(history_queries)
    )


@pytest.mark.asyncio
async def test_forgetting_to_hydrate_fails_by_name(db_session):
    """A path that skips ``attach_status_logs`` fails loudly, not mysteriously.

    ``status_logs`` is declared ``raise_on_sql`` for this reason. Without it an
    unhydrated read is a ``MissingGreenlet`` raised from inside pydantic, which
    names neither the relationship nor the route that forgot it.
    """
    _, _, booking, _addr = await _booking(db_session)
    # The factory pre-hydrates `status_logs` so ordinary tests can serialise
    # what they build. Expire just that attribute to get back the state a
    # forgetful route would produce: loaded object, unloaded relationship.
    db_session.expire(booking, ["status_logs"])

    with pytest.raises(Exception) as excinfo:
        BookingResponse.for_viewer(booking, viewer_id=booking.consumer_id)

    assert "status_logs" in str(excinfo.value)


# ------------------------------------------------------------------
# The transition just made
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_response_includes_the_transition_it_just_made(
    client, auth_headers, db_session
):
    """Accepting a booking returns a history that already contains ACCEPTED.

    ``transition_status`` writes its log row with ``db.add`` and does not flush
    it — the hydrator's SELECT autoflushes it into existence. That ordering is
    load-bearing and invisible: hydrating before ``_log_transition`` would
    return a response one entry short, and nothing else in the suite would
    notice.
    """
    consumer, provider, booking, _addr = await _booking(db_session, status=BookingStatus.REQUESTED)

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/accept",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    history = response.json()["status_history"]
    assert history, "expected the accept transition to be in its own response"
    assert history[-1]["to_status"] == BookingStatus.ACCEPTED.value


@pytest.mark.asyncio
async def test_unknown_booking_is_still_a_404(client, auth_headers, db_session):
    """The new field does not turn a missing booking into a 500.

    Cheap, but the hydrator runs on a list of bookings and an empty list is the
    shape it sees when the lookup failed.
    """
    consumer = await UserFactory.create(db_session, role="consumer")

    response = await client.get(
        f"/api/v1/bookings/{uuid.uuid4()}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 404, response.text
