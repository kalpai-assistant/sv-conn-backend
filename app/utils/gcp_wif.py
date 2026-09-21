"""SVC-307: bootstrap Workload Identity Federation for Vertex AI on App Runner.

At startup the backend reads the WIF client-config JSON from SSM (plain
String; the file holds URIs + audience refs, not credentials) and writes it
to a path pointed at by ``GOOGLE_APPLICATION_CREDENTIALS``. The
``google-genai`` SDK's default ADC discovery then follows that env var,
exchanges App Runner's instance-role AWS credentials for a GCP access token
via GCP STS, and impersonates the service account. All transparent.

Local dev deliberately skips this — the developer's own ADC
(``gcloud auth application-default login``) covers the same code path.

The known App Runner sharp edge: the gcloud-generated WIF config points
``credential_source`` at classic EC2 IMDS (169.254.169.254). App Runner
delivers the instance role via env vars / a container-creds URI, not IMDS.
The ``google-auth`` AWS credential provider *should* fall back to those, but
that is untested on App Runner. If the first deploy 401s at Vertex, the
follow-up is either (a) an explicit programmatic AWS credential source
passed to the SDK, or (b) a small AWS4-signing shim posting to GCP STS. See
the SVC-307 Jira comment for the diagnosis; the fix belongs here.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


def _read_ssm_string(param_name: str) -> str | None:
    """Fetch a plain-String SSM parameter using the boto3 default cred chain.

    Returns ``None`` when the param isn't set, the caller isn't AWS-authed,
    or boto3 raises for any other reason. All three are non-fatal at boot:
    the app still starts, KYC verification just runs against the developer's
    local ADC (dev) or fails cleanly on first Vertex call (misconfigured
    beta), which is preferable to a boot-time crash.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover — boto3 is a hard dep
        return None

    try:
        client = boto3.client("ssm")
        response = client.get_parameter(Name=param_name, WithDecryption=False)
        return response["Parameter"]["Value"]
    except (BotoCoreError, ClientError, KeyError) as exc:
        logger.warning(
            "gcp_wif_ssm_read_failed",
            param=param_name,
            error=str(exc),
        )
        return None


def bootstrap_gcp_credentials(
    *,
    ssm_param: str,
    dest_path: str,
) -> bool:
    """Materialize the WIF config from SSM and point ADC at it.

    Returns True when the file is in place (either freshly written or
    ``GOOGLE_APPLICATION_CREDENTIALS`` was already set by the environment
    — the local-dev path). False means "no creds wired here" — the caller
    should assume Vertex calls will fail and let the KYC verifier's
    unavailable-fallback handle it.

    Idempotent: safe to call multiple times per process. Existing
    ``GOOGLE_APPLICATION_CREDENTIALS`` wins over the SSM path so a dev's
    ``gcloud`` ADC keeps working during a hot reload after enabling this
    in ``.env``.
    """
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        logger.debug(
            "gcp_wif_bootstrap_skipped_env_set",
            existing=os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        )
        return True

    if not ssm_param:
        logger.debug("gcp_wif_bootstrap_skipped_no_ssm_param")
        return False

    config_json = _read_ssm_string(ssm_param)
    if not config_json:
        return False

    path = Path(dest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_json)
    # Restrict to owner-read to keep the file off any accidental
    # container-image capture. The file has no long-lived secret in it —
    # exchanging still requires the App Runner AWS creds — but 0600 makes
    # the intent obvious.
    path.chmod(0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    logger.info("gcp_wif_bootstrap_ok", path=str(path))
    return True
