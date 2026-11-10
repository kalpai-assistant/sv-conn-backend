"""SVC-451: purge-expired-recordings Celery beat task.

Runs nightly (see ``celery_app.beat_schedule``). For every ``call_records``
row whose ``recording_expires_at < now()`` AND ``recording_url IS NOT NULL``,
calls the Exotel delete-recording API and then clears ``recording_url``.
The row itself is kept forever — metadata is the dispute / audit evidence,
the recording blob is the regulated 90-day-retention asset.

Design decisions (ticket SVC-451):

* **Delete-then-null** per row, not bulk. A failed Exotel delete must NOT
  clear the URL — otherwise we lose the pointer to a recording we never
  actually deleted, and retention silently breaks. Per-row commit keeps a
  mid-run crash from rolling back the rows we did delete.
* **Keep the row.** ``call_records`` holds duration / status / from-to for
  disputes; the recording is just the heavyweight evidence. Retention
  only applies to the audio, not the metadata.
* **Mockable Exotel client.** The real :class:`ExotelRecordingClient`
  only builds an ``httpx.AsyncClient`` inside its method; the task
  accepts an injected client so tests exercise the full code path
  without network. SVC-447 (Exotel KYC) is a wall-clock-weeks blocker
  on real E2E; the mock keeps CI meaningful meanwhile.
* **Partial failures are visible.** A per-row API error is logged
  WARNING with the mapping / call_record id and the row is skipped —
  the next night retries it. Repeated failure for the same row is a
  known signal the admin runbook keys off.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.call_record import CallRecord
from app.services.consent import ExotelRecordingClient, RecordingDeleteClient
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


async def purge_expired_recordings_async(
    db: AsyncSession,
    *,
    client: RecordingDeleteClient,
    now: datetime | None = None,
) -> int:
    """Delete every expired recording, clear its URL, keep the row.

    Returns the count of rows whose ``recording_url`` was cleared. Failed
    deletes are logged and skipped (``recording_url`` stays set, next run
    retries).

    Separate from the Celery task entry point so tests can run the exact
    SQL under a fixture session without the ``SyncSessionLocal`` /
    ``AsyncSessionLocal`` bootstrap — same pattern SVC-449 used for the
    release-expired cron (sync + async twins).
    """
    now = now or datetime.now(UTC)
    stmt = select(CallRecord).where(
        CallRecord.recording_expires_at < now,
        CallRecord.recording_url.is_not(None),
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    if not rows:
        logger.info(
            "call_masking.purge_expired_recordings.sweep_clean",
            extra={"count": 0},
        )
        return 0

    cleared = 0
    for row in rows:
        recording_url = row.recording_url
        if recording_url is None:  # defensive; the SELECT already excludes NULL
            continue
        try:
            await client.delete_recording(recording_url)
        except Exception as exc:  # noqa: BLE001 — we deliberately catch all
            # Keep ``recording_url`` set so the next run retries. Repeated
            # failures for the same row are the alerting signal.
            logger.warning(
                "call_masking.purge_expired_recordings.delete_failed",
                extra={
                    "call_record_id": str(row.id),
                    "recording_url": recording_url,
                    "error": repr(exc),
                },
            )
            continue
        # Delete succeeded (or returned 404 = already gone). Null the URL
        # on the ORM row; the caller commits once per batch.
        row.recording_url = None
        cleared += 1

    await db.flush()
    logger.warning(
        "call_masking.purge_expired_recordings.completed",
        extra={"cleared": cleared, "total_expired": len(rows)},
    )
    return cleared


def _build_default_client() -> RecordingDeleteClient:
    """Build the production Exotel client from settings.

    Lazy so importing this module has no side effects and tests never
    need valid credentials. The settings keys do not exist yet — they
    land with SVC-447 Exotel onboarding. Until then the Celery beat
    schedule will exercise this path with empty strings; the real HTTP
    call will fail and every row will be skipped (the nightly log
    becomes the "waiting on Exotel KYC" signal).
    """
    from app.config import settings

    return ExotelRecordingClient(
        account_sid=getattr(settings, "EXOTEL_ACCOUNT_SID", ""),
        auth_token=getattr(settings, "EXOTEL_AUTH_TOKEN", ""),
    )


@celery_app.task(name="app.jobs.purge_expired_recordings.purge_expired_recordings")
def purge_expired_recordings() -> int:
    """Celery entry point. Nightly cron. Returns the count cleared."""

    async def _run() -> int:
        async with AsyncSessionLocal() as db:
            cleared = await purge_expired_recordings_async(db, client=_build_default_client())
            await db.commit()
            return cleared

    return asyncio.run(_run())
