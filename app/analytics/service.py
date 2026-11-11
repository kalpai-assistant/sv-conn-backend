"""Reads behind ``/admin/analytics/*`` (SVC-432). Definitions: ADMIN.md "Analytics".

Reads the materialized views in ``app.analytics.views`` only, never the raw
tables. The views carry no clock, so every time window is applied here against
``ref``: the views' ``as_of`` (``mv_analytics_meta.refreshed_at``), or a fixed
``now`` passed by tests.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import schemas as s
from app.analytics.views import CURRENT_VIEWS, refresh_statements

_IST = timezone(timedelta(hours=5, minutes=30))

GMV_WINDOWS = (7, 30, 90)
COHORT_WINDOWS = (7, 30, 90)
SP_COHORT_WINDOWS = (30, 60, 90)
ACTIVE_WINDOWS = (7, 30)
CHURN_IDLE_DAYS = 14
GAP_MAX_PINCODES = 30
UNKNOWN_PINCODE = "Unknown"
STATUSES = (
    "requested",
    "accepted",
    "en_route",
    "in_progress",
    "completed",
    "cancelled",
    "expired",
)


def _money(v: Decimal | float | int | None) -> float:
    return round(float(v or 0), 2)


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def _ist_date(ts: datetime) -> date:
    return ts.astimezone(_IST).date()


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_starts(today: date, weeks: int) -> list[date]:
    """The last ``weeks`` IST weeks, oldest first, the current (partial) one last."""
    current = _monday(today)
    return [current - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]


async def refresh_views(db: AsyncSession) -> None:
    """Refresh every analytics view in the caller's transaction (tests use this;
    the Celery task does the same through the sync session)."""
    for stmt in refresh_statements(CURRENT_VIEWS):
        await db.execute(text(stmt))


class AnalyticsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _meta(self) -> tuple[datetime, datetime | None]:
        row = (
            await self.db.execute(
                text("SELECT refreshed_at, first_booking_at FROM mv_analytics_meta")
            )
        ).one()
        return row.refreshed_at, row.first_booking_at

    # --- Overview -----------------------------------------------------------

    async def overview(self, weeks: int = 12, now: datetime | None = None) -> s.OverviewResponse:
        as_of, first_booking_at = await self._meta()
        ref = now or as_of
        today = _ist_date(ref)
        wk = week_starts(today, weeks)
        since = min(wk[0], today - timedelta(days=2 * max(GMV_WINDOWS) - 1))

        rows = (
            await self.db.execute(
                text(
                    "SELECT day, status, bookings, gmv, commission, refunded "
                    "FROM mv_bookings_daily WHERE day BETWEEN :since AND :today"
                ),
                {"since": since, "today": today},
            )
        ).all()

        # day -> totals over the completed rows; day -> status counts.
        gmv_by_day: dict[date, Decimal] = defaultdict(Decimal)
        comm_by_day: dict[date, Decimal] = defaultdict(Decimal)
        done_by_day: dict[date, int] = defaultdict(int)
        counts: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in rows:
            gmv_by_day[r.day] += r.gmv
            comm_by_day[r.day] += r.commission
            if r.status == "completed":
                done_by_day[r.day] += r.bookings
            counts[r.day][r.status] += r.bookings
            counts[r.day]["refunded"] += r.refunded

        def span(start: date, end: date) -> tuple[Decimal, int]:
            g, c, d = Decimal(0), 0, start
            while d <= end:
                g += gmv_by_day.get(d, Decimal(0))
                c += done_by_day.get(d, 0)
                d += timedelta(days=1)
            return g, c

        first_day = _ist_date(first_booking_at) if first_booking_at else None
        gmv_windows = []
        for n in GMV_WINDOWS:
            cur_start = today - timedelta(days=n - 1)
            prev_start = cur_start - timedelta(days=n)
            gmv, done = span(cur_start, today)
            previous = None
            if first_day is not None and first_day <= prev_start:
                previous, _ = span(prev_start, cur_start - timedelta(days=1))
            gmv_windows.append(
                s.GmvWindow(
                    days=n,
                    gmv=_money(gmv),
                    completed=done,
                    previous_gmv=None if previous is None else _money(previous),
                    change_pct=(
                        None if not previous else round(float((gmv - previous) / previous), 4)
                    ),
                )
            )

        weekly = []
        for ws in wk:
            g, c = span(ws, ws + timedelta(days=6))
            comm = sum(
                (comm_by_day.get(ws + timedelta(days=i), Decimal(0)) for i in range(7)),
                Decimal(0),
            )
            weekly.append(
                s.OverviewWeek(
                    week_start=ws,
                    gmv=_money(g),
                    completed=c,
                    commission=_money(comm),
                    take_rate=_ratio(float(comm), float(g)),
                    aov=None if not c else _money(g / c),
                )
            )

        daily = []
        d = wk[0]
        while d <= today:
            day_counts = counts.get(d, {})
            daily.append(
                s.BookingsDay(
                    day=d,
                    refunded=day_counts.get("refunded", 0),
                    **{st: day_counts.get(st, 0) for st in STATUSES},
                )
            )
            d += timedelta(days=1)

        cohort_rows = (
            await self.db.execute(
                text(
                    "SELECT signup_week, anchor_at, next_booking_at FROM mv_consumer_rebook "
                    "WHERE signup_week BETWEEN :start AND :today"
                ),
                {"start": wk[0], "today": today},
            )
        ).all()
        by_week: dict[date, list] = defaultdict(list)
        for r in cohort_rows:
            by_week[r.signup_week].append(r)

        cohorts = []
        for ws in wk:
            members = by_week.get(ws)
            if not members:
                continue
            cells = {}
            for n in COHORT_WINDOWS:
                window = timedelta(days=n)
                matured = [m for m in members if m.anchor_at + window <= ref]
                rebooked = sum(
                    1
                    for m in matured
                    if m.next_booking_at is not None and m.next_booking_at <= m.anchor_at + window
                )
                cells[f"d{n}"] = s.RetentionCell(
                    matured=len(matured), rebooked=rebooked, rate=_ratio(rebooked, len(matured))
                )
            cohorts.append(s.ConsumerCohort(signup_week=ws, size=len(members), **cells))

        return s.OverviewResponse(
            as_of=as_of, gmv=gmv_windows, weekly=weekly, daily=daily, cohorts=cohorts
        )

    # --- Supply -------------------------------------------------------------

    async def supply(self, weeks: int = 12, now: datetime | None = None) -> s.SupplyResponse:
        as_of, _ = await self._meta()
        ref = now or as_of
        today = _ist_date(ref)
        wk = week_starts(today, weeks)

        return s.SupplyResponse(
            as_of=as_of,
            active=await self._active(today, wk),
            sp_cohorts=await self._sp_cohorts(ref, today, wk),
            churn=await self._churn(ref),
            supply_gap=await self._supply_gap(today, wk),
            prices=await self._prices(),
            time_to_accept=await self._time_to_accept(today, wk),
        )

    async def _active(self, today: date, wk: list[date]) -> s.ActiveSupply:
        since = min(wk[0], today - timedelta(days=max(ACTIVE_WINDOWS) - 1))
        rows = (
            await self.db.execute(
                text(
                    "SELECT kind, entity_id, day FROM mv_supply_activity_daily "
                    "WHERE day BETWEEN :since AND :today"
                ),
                {"since": since, "today": today},
            )
        ).all()

        def distinct(kind: str, start: date, end: date) -> int:
            return len({r.entity_id for r in rows if r.kind == kind and start <= r.day <= end})

        windows = {
            f"{label}_{n}d": distinct(kind, today - timedelta(days=n - 1), today)
            for kind, label in (("sp", "sps"), ("pro", "pros"))
            for n in ACTIVE_WINDOWS
        }
        weekly = [
            s.ActiveWeek(
                week_start=ws,
                active_sps=distinct("sp", ws, ws + timedelta(days=6)),
                active_pros=distinct("pro", ws, ws + timedelta(days=6)),
            )
            for ws in wk
        ]
        return s.ActiveSupply(weekly=weekly, **windows)

    async def _sp_cohorts(self, ref: datetime, today: date, wk: list[date]) -> list[s.SpCohort]:
        rows = (
            await self.db.execute(
                text(
                    "SELECT kyc_reviewed_at, booked_d30, booked_d60, booked_d90 "
                    "FROM mv_sp_lifecycle WHERE kyc_reviewed_at IS NOT NULL"
                )
            )
        ).all()
        by_week: dict[date, list] = defaultdict(list)
        for r in rows:
            kw = _monday(_ist_date(r.kyc_reviewed_at))
            if wk[0] <= kw <= today:
                by_week[kw].append(r)

        out = []
        for ws in wk:
            members = by_week.get(ws)
            if not members:
                continue
            latest = max(m.kyc_reviewed_at for m in members)
            cells: dict[str, float | None] = {}
            for n in SP_COHORT_WINDOWS:
                if latest + timedelta(days=n) > ref:
                    cells[f"d{n}"] = None
                else:
                    hit = sum(1 for m in members if getattr(m, f"booked_d{n}"))
                    cells[f"d{n}"] = _ratio(hit, len(members))
            out.append(s.SpCohort(kyc_week=ws, size=len(members), **cells))
        return out

    async def _churn(self, ref: datetime) -> list[s.ChurnMonth]:
        rows = (
            await self.db.execute(text("SELECT signup_at, last_booking_at FROM mv_sp_lifecycle"))
        ).all()
        cutoff = ref - timedelta(days=CHURN_IDLE_DAYS)
        verified: dict[str, int] = defaultdict(int)
        churned: dict[str, int] = defaultdict(int)
        for r in rows:
            month = r.signup_at.astimezone(_IST).strftime("%Y-%m")
            verified[month] += 1
            if r.last_booking_at is None or r.last_booking_at <= cutoff:
                churned[month] += 1
        return [
            s.ChurnMonth(
                signup_month=m,
                verified=verified[m],
                churned=churned[m],
                rate=round(churned[m] / verified[m], 4),
            )
            for m in sorted(verified)
        ]

    async def _supply_gap(self, today: date, wk: list[date]) -> s.SupplyGap:
        rows = (
            await self.db.execute(
                text(
                    "SELECT pincode, service_id, service_name, "
                    "sum(demand) AS demand, sum(unmet) AS unmet "
                    "FROM mv_supply_gap_weekly WHERE week_start BETWEEN :start AND :today "
                    "GROUP BY pincode, service_id, service_name"
                ),
                {"start": wk[0], "today": today},
            )
        ).all()
        pin_demand: dict[str, int] = defaultdict(int)
        for r in rows:
            pin_demand[r.pincode] += int(r.demand)
        ranked = sorted(pin_demand, key=lambda p: (-pin_demand[p], p))
        kept = [p for p in ranked if p != UNKNOWN_PINCODE][:GAP_MAX_PINCODES]
        if UNKNOWN_PINCODE in pin_demand:
            kept.append(UNKNOWN_PINCODE)
        kept_set = set(kept)

        cells, services = [], {}
        for r in rows:
            if r.pincode not in kept_set:
                continue
            services[r.service_id] = r.service_name
            cells.append(
                s.GapCell(
                    pincode=r.pincode,
                    service_id=r.service_id,
                    demand=int(r.demand),
                    unmet=int(r.unmet),
                )
            )
        cells.sort(key=lambda c: (kept.index(c.pincode), services[c.service_id]))
        return s.SupplyGap(
            pincodes=kept,
            services=[
                s.GapService(id=i, name=n) for i, n in sorted(services.items(), key=lambda x: x[1])
            ],
            cells=cells,
            truncated=len(kept) < len(pin_demand),
            total_pincodes=len(pin_demand),
        )

    async def _prices(self) -> list[s.CategoryPrices]:
        rows = (
            await self.db.execute(
                text(
                    "SELECT category_id, category_name, items, min_price, p25, median, p75, "
                    "max_price FROM mv_price_by_category ORDER BY category_name"
                )
            )
        ).all()
        return [
            s.CategoryPrices(
                category_id=r.category_id,
                name=r.category_name,
                count=r.items,
                min=_money(r.min_price),
                p25=_money(r.p25),
                median=_money(r.median),
                p75=_money(r.p75),
                max=_money(r.max_price),
            )
            for r in rows
        ]

    async def _time_to_accept(self, today: date, wk: list[date]) -> list[s.AcceptWeek]:
        rows = (
            await self.db.execute(
                text(
                    "SELECT week_start, accepted, p50_seconds, p90_seconds "
                    "FROM mv_time_to_accept_weekly WHERE week_start BETWEEN :start AND :today"
                ),
                {"start": wk[0], "today": today},
            )
        ).all()
        by_week = {r.week_start: r for r in rows}
        out = []
        for ws in wk:
            r = by_week.get(ws)
            out.append(
                s.AcceptWeek(
                    week_start=ws,
                    accepted=r.accepted if r else 0,
                    p50_seconds=round(r.p50_seconds, 1) if r else None,
                    p90_seconds=round(r.p90_seconds, 1) if r else None,
                )
            )
        return out
