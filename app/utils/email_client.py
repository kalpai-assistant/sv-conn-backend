"""Outbound email through AWS SES (SVC-486). The backend's only email sender.

``send_email`` is sync (boto3) and meant for Celery tasks. It returns
``"sent"`` or ``"mocked"`` and raises on an SES error, so the caller can record
a ``failed`` notice with the error.

**Mock mode.** With ``EMAIL_FROM`` empty, nothing is sent: the email is logged as
``[MOCK EMAIL]`` (recipient and subject only, never the body) and the call
returns ``"mocked"``. That is the default everywhere until the SES sender on
eazyserv.com is verified and has production access. Setting ``EMAIL_FROM`` turns
real sends on, with no code change.

The SES client is built lazily on the first real send and then reused, so an
import, or a test in mock mode, never builds one. Region: ``EMAIL_SES_REGION``
(Mumbai by default). Credentials come from boto3's own chain (the task role on
App Runner). Nothing here reads the S3 keys.
"""

import logging
from functools import lru_cache
from typing import Literal

from app.config import get_settings

logger = logging.getLogger(__name__)

EmailOutcome = Literal["sent", "mocked"]


@lru_cache(maxsize=1)
def _ses_client(region: str):
    import boto3

    return boto3.client("ses", region_name=region)


def send_email(*, to: str, subject: str, text: str, html: str) -> EmailOutcome:
    """Send one email to ``to``, with a plain-text and an HTML part."""
    settings = get_settings()
    if not settings.EMAIL_FROM:
        logger.info("[MOCK EMAIL] to=%s subject=%s", to, subject)
        return "mocked"

    _ses_client(settings.EMAIL_SES_REGION).send_email(
        Source=settings.EMAIL_FROM,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text, "Charset": "UTF-8"},
                "Html": {"Data": html, "Charset": "UTF-8"},
            },
        },
    )
    return "sent"
