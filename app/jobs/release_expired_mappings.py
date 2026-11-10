"""SVC-449: release-expired-mappings Celery beat task.

Runs every 15 minutes (see ``celery_app.beat_schedule``). Hard-releases
call_mappings whose ``expires_at`` has passed with ``released_at`` still
NULL — the "stuck booking" path: a booking that never went through
complete/cancel so the normal release hook never fired.

The sweep is additive to the normal release path, NOT a replacement:
* normal release (complete/cancel) stamps ``released_at = now() + 48h``
  so the mapping is still routable for the grace window;
* this sweep catches the case where neither event ever fires and sets
  ``released_at = now()`` so the virtual number returns to the pool.

Idempotent — a sweep over an empty result set logs and returns 0.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.services.call_masking import hard_release_expired
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.jobs.release_expired_mappings.release_expired_mappings")
def release_expired_mappings() -> int:
    """Hard-release stuck mappings. Returns the number released."""
    now = datetime.now(UTC)
    count = hard_release_expired(now)
    # Emit the metric name the alerting runbook (SVC-454) will key off.
    # Log level is INFO on a normal run and WARNING inside the helper on a
    # non-zero release — a non-zero release is interesting (stuck bookings
    # exist); a zero release is the expected happy path.
    logger.info(
        "call_masking.release_expired_mappings.completed",
        extra={"released_count": count},
    )
    return count
