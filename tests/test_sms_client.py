"""Regression tests for the MSG91 client's failure surfacing.

Pre-fix, `MSG91Client.send_otp` used `raise_for_status()` which only
catches HTTP >= 400. MSG91 v5's OTP endpoint returns HTTP 200 with the
real status in `body["type"]`, so a bad `template_id`, missing DLT
approval, or `authkey` mismatch would look successful. That lied to the
router — send-otp returned 200 while the SMS was silently dropped.

These tests pin the new contract: any provider-side rejection MUST
raise `SMSDeliveryError`, which the router surfaces as 502.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.exceptions import SMSDeliveryError
from app.utils.sms_client import MSG91Client


def _mock_response(
    status_code: int, json_body: dict | None = None, text: str = ""
) -> httpx.Response:
    """Build a real httpx.Response so the client's parsing runs against
    the same code path as production. Avoids a magic-mock that hides
    contract drift when httpx changes shape."""
    if json_body is not None:
        return httpx.Response(status_code=status_code, json=json_body)
    return httpx.Response(status_code=status_code, text=text)


@pytest.mark.asyncio
async def test_msg91_refuses_when_template_id_missing():
    """No template_id → don't even attempt the call. This is the config
    gap that took us hours to spot; the client now fails fast so the
    error surfaces in CloudWatch instead of masquerading as success."""
    with patch("app.utils.sms_client.settings") as s:
        s.MSG91_TEMPLATE_ID = ""
        s.MSG91_AUTH_KEY = "anything"
        with pytest.raises(SMSDeliveryError, match="MSG91_TEMPLATE_ID"):
            await MSG91Client().send_otp("+919999999999", "123456")


@pytest.mark.asyncio
async def test_msg91_raises_on_error_type_in_body_despite_200_status():
    """The exact regression: HTTP 200 with `type=error`. Pre-fix this
    was treated as success and the caller lied to the user."""
    with patch("app.utils.sms_client.settings") as s:
        s.MSG91_TEMPLATE_ID = "tpl_xyz"
        s.MSG91_AUTH_KEY = "key"
        with patch("app.utils.sms_client.httpx.AsyncClient") as ac:
            ac.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=_mock_response(200, {"type": "error", "message": "Invalid Template"})
            )
            with pytest.raises(SMSDeliveryError, match="Invalid Template"):
                await MSG91Client().send_otp("+919999999999", "123456")


@pytest.mark.asyncio
async def test_msg91_success_path_stays_quiet():
    """Sanity: a real success shouldn't raise. Guards against
    over-eager error handling that would break the happy path."""
    with patch("app.utils.sms_client.settings") as s:
        s.MSG91_TEMPLATE_ID = "tpl_xyz"
        s.MSG91_AUTH_KEY = "key"
        with patch("app.utils.sms_client.httpx.AsyncClient") as ac:
            ac.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=_mock_response(200, {"type": "success", "request_id": "abc"})
            )
            # No exception = pass.
            await MSG91Client().send_otp("+919999999999", "123456")


@pytest.mark.asyncio
async def test_msg91_raises_on_5xx_from_edge():
    """MSG91's edge going down (rare but real) is a delivery failure
    same as an error body — router should return 502 either way."""
    with patch("app.utils.sms_client.settings") as s:
        s.MSG91_TEMPLATE_ID = "tpl_xyz"
        s.MSG91_AUTH_KEY = "key"
        with patch("app.utils.sms_client.httpx.AsyncClient") as ac:
            ac.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=_mock_response(503, text="Service Unavailable")
            )
            with pytest.raises(SMSDeliveryError, match="503"):
                await MSG91Client().send_otp("+919999999999", "123456")
