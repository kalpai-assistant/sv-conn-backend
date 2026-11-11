"""SVC-432: refresh the admin analytics materialized views (beat, every 900 s)."""

import time

import structlog
from sqlalchemy import text

from app.analytics.views import CURRENT_VIEWS, refresh_statements
from app.tasks.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.analytics_tasks.refresh_analytics_views")
def refresh_analytics_views():
    """REFRESH ... CONCURRENTLY every view in one transaction, so they all share
    ``mv_analytics_meta.refreshed_at`` (``now()`` is the transaction start) and
    dashboard reads never block. A failure rolls back and leaves the previous
    snapshot in place; the next tick retries."""
    from app.database import SyncSessionLocal

    started = time.monotonic()
    with SyncSessionLocal() as db:
        for stmt in refresh_statements(CURRENT_VIEWS):
            db.execute(text(stmt))
        db.commit()
    logger.info(
        "analytics.views_refreshed",
        views=len(CURRENT_VIEWS),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
