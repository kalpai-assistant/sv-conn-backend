"""SVC-307: AI-based Aadhaar KYC verifier.

Sends the three KYC images (Aadhaar front, back, live selfie) to Gemini via
Vertex AI, asks for extracted fields + tampering / blurriness / selfie-match
signals, then applies the local cross-check rules (fuzzy name match, DOB
equality) to decide a final ``outcome`` — ``auto_approve``, ``needs_review``,
or ``reject``.

Auth is Workload Identity Federation, not an API key: the ``google-genai``
SDK reads ``GOOGLE_APPLICATION_CREDENTIALS`` (pointed at the WIF config
written to disk by ``app.utils.gcp_wif`` on startup), exchanges the App
Runner instance-role AWS credentials for a GCP access token via GCP STS,
and impersonates ``eazy-serv@eazy-serv-509209.iam.gserviceaccount.com``.
See the SVC-307 Jira comment for the full runbook.

On any LLM failure (timeout, 5xx after one retry, malformed response) the
verifier returns a ``needs_review`` verdict with the ``llm_unavailable``
flag rather than raising — the KYC submission itself must not fail because
of an AI outage.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from app.config import get_settings

logger = structlog.get_logger(__name__)


class KYCExtracted(BaseModel):
    """Fields the LLM extracts off the Aadhaar images.

    Every field is optional because the LLM should never invent a value it
    can't read — a blurred DOB is a diagnostic signal, and a hallucinated
    one would slip a fraudulent submission past every downstream check.
    ``aadhaar_last4`` (not the full 12-digit number) so this payload can
    safely sit on ``provider_profiles.kyc_ai_verdict`` for audit without
    duplicating the PII the encrypted column already holds.
    """

    aadhaar_last4: str | None = None
    name_en: str | None = None
    name_local: str | None = None
    dob: str | None = None  # ISO-8601 YYYY-MM-DD when the LLM can parse it
    gender: str | None = None
    address: str | None = None


class KYCLLMResponse(BaseModel):
    """Schema constraint we hand Gemini via ``response_schema``.

    Deliberately narrower than :class:`KYCVerdict` — the LLM only observes
    the images, so it doesn't get to decide the final ``outcome``. That
    lands here in Python after we merge its extraction + signals with the
    fuzzy-name / DOB / reject-flag rules.
    """

    extracted: KYCExtracted = Field(default_factory=KYCExtracted)
    selfie_matches_aadhaar: bool = False
    selfie_match_confidence: float = 0.0  # 0.0–1.0
    is_aadhaar: bool = True
    is_tampered: bool = False
    is_blurry_front: bool = False
    is_blurry_back: bool = False
    is_blurry_selfie: bool = False
    notes: str | None = None


class KYCVerdict(BaseModel):
    """Final verdict written to ``provider_profiles.kyc_ai_verdict``.

    ``outcome`` is the machine-readable answer the auto-transition path
    branches on; ``flags`` is the human-readable trail the admin UI shows
    when the verdict lands in the review queue. ``raw_response`` is the
    full LLM response for after-the-fact audit — mostly a debugging aid.
    """

    outcome: Literal["auto_approve", "needs_review", "reject"]
    confidence: float
    extracted: dict[str, Any]
    flags: list[str]
    raw_response: dict[str, Any]


_PROMPT = """You are a KYC document verifier for a services marketplace in India.

Three images are attached in this order:
  1. Aadhaar card FRONT (photo side with the person's photo, name, DOB, gender, and Aadhaar number)
  2. Aadhaar card BACK (address side)
  3. Live selfie of the person submitting

Return a JSON object matching the response schema. Rules:

- ``extracted``: read the visible text off the Aadhaar front + back. Populate
  ``aadhaar_last4`` with ONLY the last 4 digits of the 12-digit Aadhaar number,
  never the full number. If any field is not clearly readable, leave it null;
  do not guess. ``dob`` must be ISO-8601 (YYYY-MM-DD).
- ``selfie_matches_aadhaar``: true only if the person in the selfie is clearly
  the same as the photo on the Aadhaar front. Consider face shape, features,
  and hairline. Small differences due to age, glasses, or facial hair are OK.
- ``selfie_match_confidence``: your 0.0–1.0 confidence in the above boolean.
- ``is_aadhaar``: false if the front or back image is clearly not an Indian
  Aadhaar card (e.g. driving licence, PAN, blank sheet).
- ``is_tampered``: true if you see signs of digital editing, mismatched
  fonts, pasted photos, or physical modification.
- ``is_blurry_*``: true if the corresponding image is too blurry to read
  key fields (front / back) or to see the face (selfie).
- ``notes``: one short sentence if anything is worth flagging that doesn't
  fit the fields above; otherwise null.

Be conservative. When unsure, prefer null / false over a confident wrong
answer — a needs_review verdict downstream is cheap; a wrong auto-approve
is not.
"""


class KYCVerifier:
    """Runs the LLM verification and applies the local cross-check rules."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._model = model or settings.GEMINI_MODEL
        self._project = settings.GCP_PROJECT_ID
        self._location = settings.GCP_LOCATION
        self._timeout_seconds = settings.AI_KYC_LLM_TIMEOUT_SECONDS
        # Client is injectable for tests — the real one is created lazily so
        # the module import doesn't fail when google-genai's ADC discovery
        # can't find creds (e.g. in the standalone test suite).
        self._client = client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify(
        self,
        *,
        front_bytes: bytes,
        back_bytes: bytes,
        selfie_bytes: bytes,
        stated_name: str,
        stated_dob: date | None,
    ) -> KYCVerdict:
        """Score a KYC submission end-to-end.

        Never raises: on any LLM failure (timeout, 5xx after retry, malformed
        response) the verdict is ``needs_review`` with the ``llm_unavailable``
        flag, so the submission itself still succeeds and lands in the
        admin queue.
        """
        started = time.monotonic()
        llm_response, llm_error = await self._call_llm_with_retry(
            front_bytes=front_bytes,
            back_bytes=back_bytes,
            selfie_bytes=selfie_bytes,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if llm_error is not None or llm_response is None:
            logger.warning(
                "kyc_verifier_llm_unavailable",
                error=str(llm_error) if llm_error else "no_response",
                elapsed_ms=elapsed_ms,
            )
            return KYCVerdict(
                outcome="needs_review",
                confidence=0.0,
                extracted={},
                flags=["llm_unavailable"],
                raw_response={"error": str(llm_error) if llm_error else "no_response"},
            )

        return self._decide(
            llm_response=llm_response,
            stated_name=stated_name,
            stated_dob=stated_dob,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # LLM plumbing
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        # Deferred import: google-genai's ADC discovery runs on Client()
        # construction and will hard-fail if the WIF config isn't in place.
        # The test suite passes a mock client into the constructor and
        # never hits this branch.
        from google import genai

        self._client = genai.Client(
            vertexai=True,
            project=self._project,
            location=self._location,
        )
        return self._client

    async def _call_llm_with_retry(
        self,
        *,
        front_bytes: bytes,
        back_bytes: bytes,
        selfie_bytes: bytes,
    ) -> tuple[KYCLLMResponse | None, Exception | None]:
        """One retry on any exception — matches the ticket's "retry once on 5xx"
        rule, widened to any transport failure because google-genai raises
        distinct exception classes for those which we don't want to fan out
        the case list to catch individually. Retryable classification lives
        in ``_is_retryable``.
        """
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._invoke,
                        front_bytes=front_bytes,
                        back_bytes=back_bytes,
                        selfie_bytes=selfie_bytes,
                    ),
                    timeout=self._timeout_seconds,
                )
                return response, None
            except Exception as exc:  # noqa: BLE001 — logged + rewrapped
                last_error = exc
                if attempt == 0 and self._is_retryable(exc):
                    logger.info(
                        "kyc_verifier_llm_retry",
                        error=str(exc),
                        attempt=attempt + 1,
                    )
                    continue
                break
        return None, last_error

    def _invoke(
        self,
        *,
        front_bytes: bytes,
        back_bytes: bytes,
        selfie_bytes: bytes,
    ) -> KYCLLMResponse:
        """Blocking Vertex call — always run under ``asyncio.to_thread``.

        The Vertex ``contents`` list requires ``role="user"`` on every entry;
        omitting it returns a 400 (see SVC-307 runbook comment). The
        ``response_schema=KYCLLMResponse`` config asks the SDK to validate +
        parse the response into the pydantic model for us; ``resp.parsed``
        is the typed object.
        """
        from google.genai import types  # deferred, matches _get_client

        client = self._get_client()
        content = types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=_PROMPT),
                types.Part.from_bytes(data=front_bytes, mime_type="image/jpeg"),
                types.Part.from_bytes(data=back_bytes, mime_type="image/jpeg"),
                types.Part.from_bytes(data=selfie_bytes, mime_type="image/jpeg"),
            ],
        )
        response = client.models.generate_content(
            model=self._model,
            contents=[content],
            config={
                "response_mime_type": "application/json",
                "response_schema": KYCLLMResponse,
            },
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, KYCLLMResponse):
            return parsed
        # Fallback: the SDK didn't parse for us (older version or a version
        # that returns a dict). Coerce through pydantic so a malformed shape
        # raises here and gets caught by the retry / needs_review path.
        if isinstance(parsed, dict):
            return KYCLLMResponse.model_validate(parsed)
        # Last-ditch: response.text is the raw JSON string.
        text = getattr(response, "text", None)
        if isinstance(text, str) and text:
            import json

            return KYCLLMResponse.model_validate(json.loads(text))
        raise RuntimeError("Gemini response could not be parsed into KYCLLMResponse")

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        # asyncio.TimeoutError from wait_for wraps to TimeoutError in 3.11+.
        if isinstance(exc, TimeoutError | asyncio.TimeoutError):
            return True
        # google-genai wraps HTTP errors with a status_code attribute on
        # the exception; retry on 5xx. Attribute check keeps us decoupled
        # from the exact exception class (which has moved between SDK
        # versions).
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(code, int) and 500 <= code < 600:
            return True
        return False

    # ------------------------------------------------------------------
    # Decision rules
    # ------------------------------------------------------------------

    def _decide(
        self,
        *,
        llm_response: KYCLLMResponse,
        stated_name: str,
        stated_dob: date | None,
        elapsed_ms: int,
    ) -> KYCVerdict:
        flags: list[str] = []
        extracted = llm_response.extracted
        raw = llm_response.model_dump()

        # 1) Hard-reject signals — LLM says this isn't Aadhaar or is tampered.
        if not llm_response.is_aadhaar:
            flags.append("not_aadhaar")
        if llm_response.is_tampered:
            flags.append("tampered")

        if flags and (not llm_response.is_aadhaar or llm_response.is_tampered):
            logger.info("kyc_verifier_reject", flags=flags, elapsed_ms=elapsed_ms)
            return KYCVerdict(
                outcome="reject",
                confidence=1.0 - llm_response.selfie_match_confidence,
                extracted=extracted.model_dump(),
                flags=flags,
                raw_response=raw,
            )

        # 2) Image-quality signals. These don't reject on their own — they
        # push to needs_review so the admin sees the docs and can decide.
        if llm_response.is_blurry_front:
            flags.append("blurry_front")
        if llm_response.is_blurry_back:
            flags.append("blurry_back")
        if llm_response.is_blurry_selfie:
            flags.append("blurry_selfie")

        # 3) Name match (rapidfuzz token_set_ratio — ordering-insensitive,
        # handles "Ravi Kumar" vs "Kumar, Ravi"). Missing extracted name is
        # its own flag rather than a zero-score, so a needs_review verdict
        # can be reasoned about ("we couldn't read the name" vs "names
        # don't match").
        name_score = 0
        if extracted.name_en and stated_name:
            name_score = fuzz.token_set_ratio(extracted.name_en, stated_name)
            if name_score < 70:
                flags.append("name_mismatch")
            elif name_score < 85:
                flags.append("name_close")
        elif stated_name and not extracted.name_en:
            flags.append("name_unreadable")

        # 4) DOB check — exact match only when both sides have a value.
        # ``stated_dob=None`` is legitimate (older onboarding flows didn't
        # collect it) and shouldn't count against the submission.
        if stated_dob is not None and extracted.dob:
            try:
                extracted_dob = datetime.strptime(extracted.dob, "%Y-%m-%d").date()
                if extracted_dob != stated_dob:
                    flags.append("dob_mismatch")
            except ValueError:
                flags.append("dob_unparseable")

        # 5) Selfie signal — trust the LLM's boolean + confidence.
        if not llm_response.selfie_matches_aadhaar:
            flags.append("selfie_no_match")
        elif llm_response.selfie_match_confidence < 0.85:
            flags.append("selfie_low_match")

        # 6) All-fields-present gate for auto-approve.
        required_fields = ("aadhaar_last4", "name_en", "gender")
        missing = [f for f in required_fields if not getattr(extracted, f)]
        if missing:
            flags.append("extraction_incomplete")

        # 7) Final outcome.
        blocking_flags = {
            "name_mismatch",
            "name_unreadable",
            "dob_mismatch",
            "dob_unparseable",
            "selfie_no_match",
            "selfie_low_match",
            "blurry_front",
            "blurry_back",
            "blurry_selfie",
            "extraction_incomplete",
        }
        has_blocking = any(f in blocking_flags for f in flags)

        outcome: Literal["auto_approve", "needs_review", "reject"]
        if has_blocking:
            outcome = "needs_review"
        else:
            outcome = "auto_approve"

        # ``confidence`` reflects the strongest signal we could measure:
        # selfie match times name-match score (normalised). Purely for the
        # admin UI's "how sure was the AI" annotation.
        confidence = llm_response.selfie_match_confidence * (
            name_score / 100 if name_score else 1.0
        )

        logger.info(
            "kyc_verifier_decided",
            outcome=outcome,
            flags=flags,
            name_score=name_score,
            selfie_confidence=llm_response.selfie_match_confidence,
            elapsed_ms=elapsed_ms,
        )

        return KYCVerdict(
            outcome=outcome,
            confidence=round(confidence, 3),
            extracted=extracted.model_dump(),
            flags=flags,
            raw_response=raw,
        )


# ----------------------------------------------------------------------
# SSM-backed auto-approve flag lookup
# ----------------------------------------------------------------------


_AUTO_APPROVE_CACHE: dict[str, tuple[float, bool]] = {}
_AUTO_APPROVE_TTL_SECONDS = 60.0


def get_auto_approve_enabled() -> bool:
    """Read ``AI_KYC_AUTO_APPROVE`` at request-handling time, not boot.

    The runbook comment on SVC-307 asks for this: flipping the beta flag on
    SSM should not require a redeploy. The lookup caches for 60 seconds so
    a burst of submissions doesn't fan out to SSM. Falls back to the
    boot-time ``settings.AI_KYC_AUTO_APPROVE`` when the SSM param isn't
    configured (local dev / tests).
    """
    settings = get_settings()
    param = settings.AI_KYC_AUTO_APPROVE_SSM_PARAM
    if not param:
        return settings.AI_KYC_AUTO_APPROVE

    now = time.monotonic()
    cached = _AUTO_APPROVE_CACHE.get(param)
    if cached and (now - cached[0]) < _AUTO_APPROVE_TTL_SECONDS:
        return cached[1]

    from app.utils.gcp_wif import _read_ssm_string

    raw = _read_ssm_string(param)
    enabled = (raw or "").strip().lower() in {"true", "1", "yes"}
    _AUTO_APPROVE_CACHE[param] = (now, enabled)
    return enabled
