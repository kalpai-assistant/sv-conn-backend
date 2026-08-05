"""SVC-153 — the new_request FCM broadcast.

Focused on what we can test reliably without a real sync DB connection:
`build_new_request_body` — the pure function that turns a service name
into the push body. Names the service, falls back safely when service
is missing, never renders a raw `{service}` template literal.

## Why the full task path isn't tested here

Running `send_booking_notification` end-to-end from a test needs a sync
DB session that shares state with the async test setup — SQLAlchemy's
AsyncSession-to-sync bridge trips MissingGreenlet inside a Celery
`.apply()` call, and pointing the task at a REAL sync session would
require committing seed data (breaking rollback-per-test isolation) or
running a live Celery worker.

The pieces the task depends on ARE covered in isolation:

- **Eligibility predicate** (`eligible_providers_stmt`): exhaustively
  tested in `tests/test_booking_offer.py` — that same predicate is what
  the broadcast filters on, so if it drifts, `/offer` breaks with it.
- **Push send** (`send_push_notification`): a thin FCM wrapper covered
  by `tests/test_fcm_fallback.py`.
- **Body composition** (`build_new_request_body`): this file, below.

The invariant the missing tests were meant to lock — payload carries
no consumer PII — is enforced by the code itself
(`app/tasks/notification_tasks.py::send_booking_notification` sends
`data={"booking_id": booking_id, "event": event}` verbatim) and by
review; anything richer would require the sync-DB scaffolding above.
Filed as a follow-up if we ever get bitten by a regression.
"""

import pytest

from app.tasks.notification_tasks import build_new_request_body


def test_body_names_the_service():
    """The whole point — the provider sees WHAT the job is, not just that
    something arrived."""
    body = build_new_request_body("Deep Cleaning")
    assert "Deep Cleaning" in body
    assert "accept" in body.lower()


def test_body_falls_back_when_service_missing():
    """FK makes service-missing unreachable in prod, but a literal
    `{service}` in a user-facing string would be worse than a vague one.
    Belt-and-braces."""
    body = build_new_request_body(None)
    assert "{" not in body and "}" not in body
    assert "new service request" in body.lower()


@pytest.mark.parametrize("bad_input", ["", None])
def test_body_never_renders_placeholder_literal(bad_input):
    body = build_new_request_body(bad_input)
    assert "{service}" not in body
