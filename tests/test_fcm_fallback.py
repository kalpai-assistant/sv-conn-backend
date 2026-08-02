"""FCM behaviour when Firebase credentials are absent (SVC-156 groundwork).

The Firebase project does not exist yet, and even once it does, dev machines,
CI runners and this test suite will not hold its credentials — nor should they
be firing real notifications at real devices. So the unconfigured path is the
permanent behaviour outside production, not a temporary stand-in.

What matters here is the asymmetry:

* **outside production** an unset ``FCM_CREDENTIALS_PATH`` logs the push and
  carries on, so the app is usable without Firebase;
* **in production** it still raises, because silently dropping notifications
  there is invisible — providers just never hear about bookings, and there is
  no error anywhere to trace it from.

``test_production_still_raises_when_unconfigured`` is the load-bearing one. Lose
it and a deploy that forgets the credential quietly swallows every push forever.

No DB or Redis — part of the standalone set.
"""
import pytest

from app.utils import fcm_client


@pytest.fixture
def unconfigured(monkeypatch):
    """FCM with no credentials, in a non-production environment."""
    monkeypatch.setattr(fcm_client.settings, "FCM_CREDENTIALS_PATH", "", raising=False)
    monkeypatch.setattr(fcm_client.settings, "ENVIRONMENT", "development", raising=False)


def _send():
    return fcm_client.send_push_notification(
        token="device-token-abc123",
        title="New Booking Request!",
        body="Deep Cleaning — accept within 2 minutes.",
        data={"booking_id": "8f3a", "event": "new_request"},
    )


def test_unconfigured_logs_instead_of_raising(unconfigured):
    """The whole point: the app runs without Firebase credentials."""
    assert fcm_client.is_configured() is False
    assert _send() == fcm_client.NOT_SENT


def test_unconfigured_never_touches_firebase(unconfigured, monkeypatch):
    """It must short-circuit, not fail somewhere inside firebase-admin.

    A half-initialised send would surface as an opaque Firebase auth error and
    send someone hunting a credentials problem that does not exist.
    """
    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("firebase-admin was called without credentials")

    monkeypatch.setattr(fcm_client.messaging, "send", _boom)
    monkeypatch.setattr(fcm_client.credentials, "Certificate", _boom)

    assert _send() == fcm_client.NOT_SENT


def test_production_still_raises_when_unconfigured(monkeypatch):
    """Load-bearing. Production must fail loudly, never log-and-drop.

    Dropping pushes in production is undetectable from the outside: nothing
    errors, no response changes, providers simply never get told about bookings.
    Better to refuse to start sending at all.
    """
    monkeypatch.setattr(fcm_client.settings, "FCM_CREDENTIALS_PATH", "", raising=False)
    monkeypatch.setattr(fcm_client.settings, "ENVIRONMENT", "production", raising=False)

    with pytest.raises(RuntimeError, match="FCM_CREDENTIALS_PATH"):
        _send()


def test_configured_sends_for_real(monkeypatch):
    """With credentials present, the fallback is out of the way entirely."""
    monkeypatch.setattr(
        fcm_client.settings, "FCM_CREDENTIALS_PATH", "/tmp/fake-sa.json", raising=False
    )
    monkeypatch.setattr(fcm_client.settings, "ENVIRONMENT", "development", raising=False)
    monkeypatch.setattr(fcm_client, "_get_app", lambda: None)

    sent = {}

    def _capture(message):
        sent["token"] = message.token
        sent["title"] = message.notification.title
        sent["data"] = message.data
        return "projects/x/messages/123"

    monkeypatch.setattr(fcm_client.messaging, "send", _capture)

    assert fcm_client.is_configured() is True
    assert _send() == "projects/x/messages/123"
    assert sent["token"] == "device-token-abc123"
    assert sent["data"] == {"booking_id": "8f3a", "event": "new_request"}


def test_logged_payload_carries_what_a_client_needs(unconfigured, caplog):
    """The log has to be useful, or it is just a silent drop with extra steps.

    While SVC-67/68/69 are being built there is no device to inspect, so this
    log is the only view of the exact ``data`` dict the card will parse — the
    part most likely to be wrong.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        _send()

    blob = caplog.text
    assert "fcm_not_configured" in blob
    assert "new_request" in blob, "the event the client switches on is missing"
    assert "8f3a" in blob, "the booking id the card fetches with is missing"


def test_device_token_is_not_logged(unconfigured, caplog):
    """Tokens are credentials for pushing to that device — keep them out.

    The redaction processor would scrub a key containing "token" anyway; this
    pins that we never hand it one to scrub.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        _send()

    assert "device-token-abc123" not in caplog.text
