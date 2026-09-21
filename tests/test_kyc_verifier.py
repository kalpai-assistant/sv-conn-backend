"""SVC-307: unit tests for the AI KYC verifier decision logic.

Three canonical shapes the LLM can return:

- Clean extraction + selfie match → ``auto_approve``
- Blurry image / low selfie confidence → ``needs_review`` with the expected flags
- ``is_aadhaar=False`` → ``reject``, and the auto-approve path is a no-op even
  when the SSM flag is on (the cheap safety valve the ticket calls for)

Also covers the fallback: an LLM that raises → ``needs_review`` with
``llm_unavailable``, so the submit_kyc request itself never fails because
of a Vertex outage.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.kyc_verifier import (
    KYCExtracted,
    KYCLLMResponse,
    KYCVerdict,
    KYCVerifier,
)


class _StubClient:
    """Drop-in replacement for the google-genai ``Client`` used by KYCVerifier.

    Constructed with a canned :class:`KYCLLMResponse` (or an exception to
    raise). Records the ``generate_content`` call args so tests can assert
    the request shape (role=user on the content, response_schema wired up)
    without depending on the SDK's own types.
    """

    def __init__(self, *, response: KYCLLMResponse | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []
        self.models = self  # so client.models.generate_content(...) works

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return _StubResponse(parsed=self._response)


class _StubResponse:
    def __init__(self, parsed):
        self.parsed = parsed
        self.text = None


@pytest.mark.asyncio
async def test_verify_auto_approve_when_all_signals_green():
    """Clean extraction + high selfie confidence + name match → auto_approve."""
    llm = KYCLLMResponse(
        extracted=KYCExtracted(
            aadhaar_last4="1234",
            name_en="Ravi Kumar",
            dob="1990-05-14",
            gender="M",
            address="Pune, MH",
        ),
        selfie_matches_aadhaar=True,
        selfie_match_confidence=0.95,
        is_aadhaar=True,
        is_tampered=False,
    )
    verifier = KYCVerifier(client=_StubClient(response=llm))

    verdict = await verifier.verify(
        front_bytes=b"front",
        back_bytes=b"back",
        selfie_bytes=b"selfie",
        stated_name="Ravi Kumar",
        stated_dob=date(1990, 5, 14),
    )

    assert isinstance(verdict, KYCVerdict)
    assert verdict.outcome == "auto_approve"
    assert verdict.flags == []
    assert verdict.extracted["aadhaar_last4"] == "1234"
    assert verdict.confidence > 0.8


@pytest.mark.asyncio
async def test_verify_needs_review_on_blurry_front_and_low_selfie_confidence():
    """Blurry front + selfie under 0.85 → needs_review with both flags."""
    llm = KYCLLMResponse(
        extracted=KYCExtracted(
            aadhaar_last4="1234",
            name_en="Ravi Kumar",
            gender="M",
        ),
        selfie_matches_aadhaar=True,
        selfie_match_confidence=0.55,
        is_aadhaar=True,
        is_tampered=False,
        is_blurry_front=True,
    )
    verifier = KYCVerifier(client=_StubClient(response=llm))

    verdict = await verifier.verify(
        front_bytes=b"front",
        back_bytes=b"back",
        selfie_bytes=b"selfie",
        stated_name="Ravi Kumar",
        stated_dob=None,
    )

    assert verdict.outcome == "needs_review"
    assert "blurry_front" in verdict.flags
    assert "selfie_low_match" in verdict.flags


@pytest.mark.asyncio
async def test_verify_reject_on_not_aadhaar_never_auto_approves():
    """is_aadhaar=False → reject regardless of every other signal being green.

    Confirms the "rejects NEVER auto-transition" invariant at the verifier
    level: even a perfect selfie match and clean name lands in the reject
    bucket when the LLM says the doc isn't actually an Aadhaar.
    """
    llm = KYCLLMResponse(
        extracted=KYCExtracted(name_en="Ravi Kumar"),
        selfie_matches_aadhaar=True,
        selfie_match_confidence=0.99,
        is_aadhaar=False,  # not-Aadhaar signal
        is_tampered=False,
    )
    verifier = KYCVerifier(client=_StubClient(response=llm))

    verdict = await verifier.verify(
        front_bytes=b"front",
        back_bytes=b"back",
        selfie_bytes=b"selfie",
        stated_name="Ravi Kumar",
        stated_dob=None,
    )

    assert verdict.outcome == "reject"
    assert "not_aadhaar" in verdict.flags


@pytest.mark.asyncio
async def test_verify_llm_unavailable_returns_needs_review():
    """An LLM that keeps raising → needs_review flagged llm_unavailable.

    The submit_kyc request must NOT fail because of a Vertex outage — the
    provider's submission still lands in the admin queue.
    """
    verifier = KYCVerifier(client=_StubClient(exc=RuntimeError("vertex 503")))

    verdict = await verifier.verify(
        front_bytes=b"front",
        back_bytes=b"back",
        selfie_bytes=b"selfie",
        stated_name="Ravi Kumar",
        stated_dob=None,
    )

    assert verdict.outcome == "needs_review"
    assert "llm_unavailable" in verdict.flags


@pytest.mark.asyncio
async def test_verify_name_mismatch_flagged():
    """Stated name doesn't match extracted — flagged, needs_review."""
    llm = KYCLLMResponse(
        extracted=KYCExtracted(
            aadhaar_last4="1234",
            name_en="Ravi Kumar",
            gender="M",
        ),
        selfie_matches_aadhaar=True,
        selfie_match_confidence=0.95,
        is_aadhaar=True,
    )
    verifier = KYCVerifier(client=_StubClient(response=llm))

    verdict = await verifier.verify(
        front_bytes=b"front",
        back_bytes=b"back",
        selfie_bytes=b"selfie",
        stated_name="Priya Sharma",  # totally different name
        stated_dob=None,
    )

    assert verdict.outcome == "needs_review"
    assert "name_mismatch" in verdict.flags
