"""
Log secret redaction tests (SC-50 preventive control).

Every branch of the redaction logic gets its own test — silent regression
here means real secrets landing in aggregate logs, which is exactly the
class of leak that's easiest to miss in code review.

Tests exercise the redaction processor directly (pure function, no logging
plumbing needed) plus one end-to-end via structlog to prove the processor
is actually wired into the chain.
"""
import io
import json
import logging

from app.logging_config import (
    _REDACTED,
    _key_is_sensitive,
    _redact,
    _redact_secrets,
    _value_is_sensitive,
    configure_logging,
)

# ---------------------------------------------------------------------------
# _key_is_sensitive — substring matching, case-insensitive
# ---------------------------------------------------------------------------


class TestKeySensitivity:
    def test_exact_match(self):
        assert _key_is_sensitive("password") is True
        assert _key_is_sensitive("otp") is True
        assert _key_is_sensitive("token") is True

    def test_substring_match(self):
        """`access_token`, `refreshToken`, `RAZORPAY_SECRET` all catch."""
        assert _key_is_sensitive("access_token") is True
        assert _key_is_sensitive("refresh_token") is True
        assert _key_is_sensitive("RAZORPAY_SECRET") is True
        assert _key_is_sensitive("webhook_secret") is True
        assert _key_is_sensitive("doorstep_otp") is True

    def test_case_insensitive(self):
        assert _key_is_sensitive("PASSWORD") is True
        assert _key_is_sensitive("Authorization") is True
        assert _key_is_sensitive("Api_Key") is True

    def test_non_sensitive_keys_pass(self):
        """Common non-sensitive keys must NOT trigger redaction."""
        for key in ("user_id", "phone", "booking_id", "status", "event", "request_id"):
            assert _key_is_sensitive(key) is False, f"{key} wrongly flagged"


# ---------------------------------------------------------------------------
# _value_is_sensitive — pattern matching on scalar values
# ---------------------------------------------------------------------------


class TestValueSensitivity:
    def test_jwt_is_redacted(self):
        """A raw JWT accidentally logged (bad `f"..." % token`) must be caught."""
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0IiwibmFtZSI6IkFsaWNlIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        assert _value_is_sensitive(jwt) is True

    def test_razorpay_live_key_is_redacted(self):
        assert _value_is_sensitive("rzp_live_abc123XYZ") is True

    def test_razorpay_test_key_is_ok(self):
        """Test keys are safe to log — they're published in Razorpay dashboards."""
        assert _value_is_sensitive("rzp_test_abc123XYZ") is False

    def test_aws_access_key_is_redacted(self):
        assert _value_is_sensitive("AKIAIOSFODNN7EXAMPLE") is True

    def test_ordinary_strings_pass_through(self):
        for value in ("hello", "+919999900001", "booking-uuid-123", "some description"):
            assert _value_is_sensitive(value) is False, f"{value!r} wrongly flagged"

    def test_non_string_values_pass_through(self):
        """Numbers, bools, None must not raise or match."""
        for value in (42, 3.14, True, False, None):
            assert _value_is_sensitive(value) is False


# ---------------------------------------------------------------------------
# _redact — recursive walk over nested structures
# ---------------------------------------------------------------------------


class TestRedactWalk:
    def test_scalar_by_value_pattern(self):
        assert _redact("rzp_live_secret123") == _REDACTED
        assert _redact("hello") == "hello"

    def test_dict_by_sensitive_key(self):
        result = _redact({"password": "hunter2", "user_id": "abc"})
        assert result == {"password": _REDACTED, "user_id": "abc"}

    def test_nested_dict(self):
        """A secret inside a headers dict two levels deep must still be redacted."""
        record = {
            "request": {
                "headers": {"Authorization": "Bearer xyz", "content-type": "json"},
                "path": "/bookings",
            },
        }
        result = _redact(record)
        assert result["request"]["headers"]["Authorization"] == _REDACTED
        assert result["request"]["headers"]["content-type"] == "json"
        assert result["request"]["path"] == "/bookings"

    def test_list_of_dicts(self):
        """Redaction descends into list elements."""
        records = [{"otp": "123456", "user": "a"}, {"otp": "999999", "user": "b"}]
        result = _redact(records)
        assert result == [
            {"otp": _REDACTED, "user": "a"},
            {"otp": _REDACTED, "user": "b"},
        ]

    def test_tuple_type_is_preserved(self):
        """A tuple stays a tuple after redaction — used by some structlog processors."""
        result = _redact(("password_hash", "abc"))
        assert isinstance(result, tuple)


# ---------------------------------------------------------------------------
# _redact_secrets — top-level structlog processor
# ---------------------------------------------------------------------------


class TestRedactProcessor:
    def test_redacts_sensitive_key_at_top_level(self):
        event = {"event": "auth", "password": "hunter2", "user_id": "u1"}
        result = _redact_secrets(None, "info", event)
        assert result == {"event": "auth", "password": _REDACTED, "user_id": "u1"}

    def test_redacts_sensitive_value_at_top_level(self):
        event = {"event": "webhook", "payload_key": "rzp_live_abc123"}
        result = _redact_secrets(None, "info", event)
        assert result["payload_key"] == _REDACTED

    def test_does_not_mutate_input(self):
        """The processor returns a new dict — the original event stays intact.

        Matters because structlog reuses event_dicts across processors; if
        we mutated in place, an earlier processor's reference would see the
        redacted form and could log the wrong thing.
        """
        original = {"password": "hunter2", "user_id": "u1"}
        _redact_secrets(None, "info", original)
        assert original == {"password": "hunter2", "user_id": "u1"}

    def test_empty_event_dict(self):
        assert _redact_secrets(None, "info", {}) == {}

    def test_preserves_event_message_field(self):
        """The `event` field is structlog's message — must survive unchanged."""
        result = _redact_secrets(None, "info", {"event": "user login"})
        assert result["event"] == "user login"


# ---------------------------------------------------------------------------
# End-to-end via structlog — proves the processor is wired into the chain
# ---------------------------------------------------------------------------


def _capture_json_logs():
    configure_logging(level="INFO", json_logs=True)
    buf = io.StringIO()
    root = logging.getLogger()
    orig = root.handlers[0]
    handler = logging.StreamHandler(buf)
    handler.setFormatter(orig.formatter)
    root.handlers[:] = [handler]

    def restore():
        root.handlers[:] = [orig]

    return buf, restore


def test_end_to_end_password_kwarg_is_redacted():
    """A structlog call with a `password=` kwarg must land redacted in JSON."""
    import structlog

    buf, restore = _capture_json_logs()
    try:
        structlog.get_logger("test").info("login attempt", password="hunter2")
    finally:
        restore()

    records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    ours = [r for r in records if r.get("event") == "login attempt"]
    assert ours
    assert ours[0]["password"] == _REDACTED


def test_end_to_end_jwt_in_value_is_redacted():
    """A raw JWT passed as any value gets caught by value-pattern match."""
    import structlog

    buf, restore = _capture_json_logs()
    try:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123def456ghi789"
        structlog.get_logger("test").info("auth done", issued=jwt)
    finally:
        restore()

    records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    ours = [r for r in records if r.get("event") == "auth done"]
    assert ours
    assert ours[0]["issued"] == _REDACTED


def test_end_to_end_non_secret_passes_through():
    """Sanity: normal fields are NOT accidentally redacted."""
    import structlog

    buf, restore = _capture_json_logs()
    try:
        structlog.get_logger("test").info(
            "booking done", user_id="u-abc", amount_paise=12300,
        )
    finally:
        restore()

    records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    ours = [r for r in records if r.get("event") == "booking done"]
    assert ours
    assert ours[0]["user_id"] == "u-abc"
    assert ours[0]["amount_paise"] == 12300
