"""SVC-432: admin console analytics, the Overview and Supply dashboards.

Admin and ops only (support gets 403). Read-only dashboard reads, so nothing is
audited, like the SVC-426 payout dashboard. Every number comes from the
materialized views in ``app.analytics.views`` (refreshed every 15 min by Celery
beat); ``as_of`` says when. Definitions: ADMIN.md "Analytics".
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.schemas import OverviewResponse, SupplyResponse
from app.analytics.service import AnalyticsService
from app.dependencies import get_db, require_roles

router = APIRouter(
    prefix="/admin/analytics",
    tags=["Admin"],
    dependencies=[Depends(require_roles("admin", "ops"))],
)

_weeks = Query(12, ge=1, le=52, description="Trend length in IST weeks, current week last.")


@router.get("/overview", response_model=OverviewResponse)
async def analytics_overview(weeks: int = _weeks, db: AsyncSession = Depends(get_db)):
    return await AnalyticsService(db).overview(weeks=weeks)


@router.get("/supply", response_model=SupplyResponse)
async def analytics_supply(weeks: int = _weeks, db: AsyncSession = Depends(get_db)):
    return await AnalyticsService(db).supply(weeks=weeks)
