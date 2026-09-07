"""
New job requests get their own Android notification channel (SVC-278 issue 6).

A provider's incoming job is the one notification they lose money by missing,
and it sounded exactly like an app-update nudge: every push was built as a bare
`messaging.Message` with no `AndroidConfig`, so Android filed them all against
the plugin's default channel.

Android takes the sound, the vibration and whether a notification interrupts
from the **channel**, not from the message. Raising FCM priority alone changes
delivery, not presentation — so the fix needs two halves that only work
together: the app creates a channel, and the server names it.

Both halves failing is loud. **One half failing is silent**: Android falls back
to the default channel and the push simply sounds ordinary, with all the code
apparently in place. That is what these tests are for.
"""

from unittest.mock import MagicMock, patch

from app.tasks.notification_tasks import JOB_REQUEST_CHANNEL_ID
from app.utils.fcm_client import _android_config


def test_the_job_request_channel_is_versioned():
    """Android freezes a channel's sound at creation.

    After the first `createChannel`, the OS ignores every later change to sound
    or importance for that id, on that install, forever — only a new id gets
    new defaults. So changing the sound means shipping a new id, and the id has
    to carry something to bump. Without the suffix, a future sound change works
    on a clean install, passes review, and does nothing on any device that
    already had the app.
    """
    assert JOB_REQUEST_CHANNEL_ID.endswith("_v1") or "_v" in JOB_REQUEST_CHANNEL_ID


def test_the_id_matches_the_app():
    """The contract with `notification-channels.ts`.

    These two constants live in different repositories and nothing links them
    at build time. If they drift, Android files the push against a channel that
    does not exist and quietly uses the default one — no error, no log, just
    the original bug back with the fix apparently shipped.

    Hardcoded on purpose: this test should fail when someone edits the constant
    without editing the app.
    """
    assert JOB_REQUEST_CHANNEL_ID == "job_requests_v1"


def test_no_channel_means_no_android_config():
    """Every other event keeps Android's defaults.

    Giving each event its own channel would leave a provider with a wall of
    equally urgent sounds and nothing marking the one that pays them.
    """
    assert _android_config(None) is None


def test_a_channel_produces_a_high_priority_config():
    config = _android_config("job_requests_v1")

    assert config is not None
    assert config.notification.channel_id == "job_requests_v1"
    # Priority governs *delivery* — it wakes a dozing device. The channel
    # governs presentation. A time-critical push needs both; they are unrelated
    # settings and neither substitutes for the other.
    assert config.priority == "high"


# ---------------------------------------------------------------------------
# Which events actually get the channel
# ---------------------------------------------------------------------------


def _push_channel_for(event: str) -> str | None:
    """Run the real task for `event` and report the channel it pushed with.

    Goes through `send_booking_notification` rather than restating the
    condition — a test that re-derives the rule it is checking passes no matter
    what the code does.
    """
    import app.tasks.notification_tasks as nt

    booking = MagicMock()
    booking.consumer_address_id = "addr"
    booking.provider_id = "p1"
    booking.consumer_id = "c1"
    party = MagicMock()
    party.fcm_token = "token"
    party.id = "p1"
    booking.provider = party
    booking.consumer = party

    address = MagicMock()
    address.location = "POINT(73.8567 18.5204)"

    db = MagicMock()
    db.get.side_effect = lambda model, _id: booking if model.__name__ == "Booking" else address
    item = MagicMock()
    item.service_id = "svc-1"
    db.execute.return_value.scalars.return_value.all.side_effect = [[item], [party]]

    session_cm = MagicMock()
    session_cm.__enter__.return_value = db

    calls = []
    with (
        patch("app.database.SyncSessionLocal", return_value=session_cm),
        patch.object(nt, "send_push_notification", side_effect=lambda **kw: calls.append(kw)),
        patch.object(nt, "build_new_request_body", return_value="A job"),
    ):
        nt.send_booking_notification(booking_id="b1", event=event)

    return calls[0]["android_channel_id"] if calls else None


def test_only_new_request_uses_the_channel():
    """One channel stands out only if the others do not have their own."""
    assert _push_channel_for("new_request") == JOB_REQUEST_CHANNEL_ID
    for event in ("accepted", "in_progress", "completed"):
        assert _push_channel_for(event) is None, f"{event} should stay on the default channel"


def test_the_broadcast_names_the_channel_when_it_pushes():
    """End-to-end through the real task: a job push must carry the channel.

    The other tests check the pieces; this one checks they are wired together,
    which is the part that silently regresses if the argument is dropped at the
    call site.
    """
    import app.tasks.notification_tasks as nt

    booking = MagicMock()
    booking.consumer_address_id = "addr"
    provider = MagicMock()
    provider.fcm_token = "provider-token"

    address = MagicMock()
    address.location = "POINT(73.8567 18.5204)"

    db = MagicMock()
    db.get.side_effect = lambda model, _id: booking if model.__name__ == "Booking" else address

    item = MagicMock()
    item.service_id = "svc-1"
    db.execute.return_value.scalars.return_value.all.side_effect = [[item], [provider]]

    session_cm = MagicMock()
    session_cm.__enter__.return_value = db

    calls = []
    with (
        patch("app.database.SyncSessionLocal", return_value=session_cm),
        patch.object(nt, "send_push_notification", side_effect=lambda **kw: calls.append(kw)),
        patch.object(nt, "build_new_request_body", return_value="A job"),
    ):
        nt.send_booking_notification(booking_id="b1", event="new_request")

    assert calls, "the broadcast pushed nothing"
    assert calls[0]["android_channel_id"] == JOB_REQUEST_CHANNEL_ID
