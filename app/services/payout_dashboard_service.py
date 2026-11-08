"""
Admin payouts dashboard reads (SVC-426): per-SP settlement buckets, the bookings
behind them, and the accounting CSV.

Read-only. The money rules stay in ``PayoutService``: the pending bucket is its
``_owed_filter``, the scope is its ``_payout_scope_filter``, and ``payable_paise``
is the same wallet figure the trigger's estimate returns. Nothing here locks rows,
moves money or writes an audit row.
"""

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import String, and_, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, SettlementStatus
from app.models.payout_audit import PayoutAudit
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import User
from app.services.ledger_service import wallet_balance_paise
from app.services.payout_service import PayoutService
from app.services.provider_service import ProviderService
from app.utils.money import to_paise

# India has no DST, so a fixed offset is exact (same as provider_service._IST).
_IST = timezone(timedelta(hours=5, minutes=30))

# A payout attempt whose latest audit row says one of these didn't pay the SP.
# "unknown" is a lost RazorpayX response: worth showing, since it blocks the next
# attempt until it is reconciled. "refused" is a payout the task turned down
# before RazorpayX (KYC, no payout details, the cap...); its error says why.
_FAILED_OUTCOMES = ("failed", "rejected", "reversed", "unknown", "refused")

CSV_COLUMNS = (
    "sp_id",
    "sp_name",
    "booking_id",
    "locked_price",
    "commission",
    "sp_payout",
    "booking_completed_at",
    "payout_status",
)

# A spreadsheet runs a cell that starts with one of these as a formula. Tab and CR
# are on OWASP's list too.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: str) -> str:
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


def _rupees(value: Decimal) -> str:
    return f"{value:.2f}"


class PayoutDashboardService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_providers(
        self, *, status: SettlementStatus | None, page: int, per_page: int
    ) -> dict:
        """One row per SP with any payout-scope booking, largest pending debt first.

        ``status`` keeps only SPs with at least one booking in that bucket. Rows and
        ``total`` come from one query (``COUNT(*) OVER ()``), so they read the same
        snapshot, as in ``PayoutService.list_pending_settlements``.
        """
        amount = Booking.provider_payout + Booking.tip_amount
        buckets = {
            SettlementStatus.PENDING: and_(*PayoutService._owed_filter()),
            **{
                s: Booking.settlement_status == s
                for s in SettlementStatus
                if s is not SettlementStatus.PENDING
            },
        }
        bucket_cols = []
        for s, cond in buckets.items():
            bucket_cols.append(func.count(Booking.id).filter(cond).label(f"{s.value}_count"))
            bucket_cols.append(
                func.coalesce(func.sum(amount).filter(cond), 0).label(f"{s.value}_rupees")
            )
        pending_rupees = func.coalesce(
            func.sum(amount).filter(buckets[SettlementStatus.PENDING]), 0
        )

        # The profile is 1:1 and grouped away, so aggregate it, as pending-settlements
        # does. A LEFT join keeps an SP with no profile row visible.
        stmt = (
            select(
                User.id,
                User.name,
                User.phone,
                User.is_active,
                func.max(ProviderProfile.business_name).label("business_name"),
                func.bool_or(ProviderProfile.payout_method.is_not(None)).label("payout_set"),
                func.max(func.lower(func.cast(ProviderProfile.kyc_status, String))).label(
                    "kyc_status"
                ),
                *bucket_cols,
                func.count().over().label("total_providers"),
            )
            .select_from(Booking)
            .join(User, User.id == Booking.provider_id)
            .outerjoin(ProviderProfile, ProviderProfile.user_id == User.id)
            .where(*PayoutService._payout_scope_filter())
            .group_by(User.id, User.name, User.phone, User.is_active)
            .order_by(pending_rupees.desc(), User.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        if status is not None:
            stmt = stmt.having(func.count(Booking.id).filter(buckets[status]) > 0)
        rows = (await self.db.execute(stmt)).all()

        ids = [row.id for row in rows]
        reasons = await self._latest_failure_reasons(ids)

        providers = []
        for row in rows:
            entry = {
                "provider_id": row.id,
                "name": row.name,
                "business_name": row.business_name,
                "phone": row.phone,
                "is_active": row.is_active,
                "payout_set": bool(row.payout_set),
                "kyc_status": row.kyc_status or KYCStatus.PENDING.value,
            }
            earned_count = earned_paise = 0
            for s in SettlementStatus:
                count = getattr(row, f"{s.value}_count")
                paise = to_paise(getattr(row, f"{s.value}_rupees"))
                entry[f"{s.value}_count"] = count
                entry[f"{s.value}_paise"] = paise
                earned_count += count
                earned_paise += paise
            entry["earned_count"] = earned_count
            entry["earned_paise"] = earned_paise
            # max(0, wallet): what the wallet could pay right now. The Pay out button's
            # preflight sends min(wallet, pending batch), and refuses at wallet <= 0.
            entry["payable_paise"] = max(0, await wallet_balance_paise(self.db, row.id))
            entry["latest_failure_reason"] = reasons.get(row.id)
            providers.append(entry)

        # No row on this page means either nothing matches or the page is past the end.
        total = rows[0].total_providers if rows else await self._count_providers(status, buckets)
        return {"providers": providers, "total": total, "page": page, "per_page": per_page}

    async def _count_providers(self, status, buckets) -> int:
        inner = (
            select(Booking.provider_id)
            .where(*PayoutService._payout_scope_filter())
            .group_by(Booking.provider_id)
        )
        if status is not None:
            inner = inner.having(func.count(Booking.id).filter(buckets[status]) > 0)
        return (
            await self.db.execute(select(func.count()).select_from(inner.subquery()))
        ).scalar_one()

    async def _latest_failure_reasons(self, provider_ids: list[UUID]) -> dict[UUID, str]:
        """The RazorpayX reason ops needs inline, per SP.

        A booking still FAILED or REVERSED carries RazorpayX's own rejection text, so
        the newest of those wins. Otherwise the SP's latest payout attempt counts only
        if it failed: an older error that a later payout fixed is just noise.
        """
        if not provider_ids:
            return {}
        booking_rows = await self.db.execute(
            select(Booking.provider_id, Booking.settlement_failure_reason)
            .where(
                Booking.provider_id.in_(provider_ids),
                *PayoutService._payout_scope_filter(),
                Booking.settlement_status.in_((SettlementStatus.FAILED, SettlementStatus.REVERSED)),
                Booking.settlement_failure_reason.is_not(None),
            )
            .distinct(Booking.provider_id)
            .order_by(Booking.provider_id, Booking.updated_at.desc(), Booking.id.desc())
        )
        reasons = {pid: reason for pid, reason in booking_rows.all()}

        missing = [pid for pid in provider_ids if pid not in reasons]
        if missing:
            audit_rows = await self.db.execute(
                select(PayoutAudit.provider_id, PayoutAudit.outcome, PayoutAudit.error)
                .where(PayoutAudit.provider_id.in_(missing))
                .distinct(PayoutAudit.provider_id)
                .order_by(
                    PayoutAudit.provider_id, PayoutAudit.created_at.desc(), PayoutAudit.id.desc()
                )
            )
            for pid, outcome, error in audit_rows.all():
                if outcome in _FAILED_OUTCOMES and error:
                    reasons[pid] = error
        return reasons

    async def list_bookings(
        self,
        provider_id: UUID,
        *,
        settlement_status: SettlementStatus | None,
        page: int,
        per_page: int,
    ) -> dict:
        """One SP's payout-scope bookings, newest completion first."""
        first = ProviderService._first_completed()
        where = [Booking.provider_id == provider_id, *PayoutService._payout_scope_filter()]
        if settlement_status is not None:
            where.append(Booking.settlement_status == settlement_status)

        total = (await self.db.execute(select(func.count(Booking.id)).where(*where))).scalar_one()
        rows = (
            await self.db.execute(
                select(Booking, first.c.completed_at, Professional.display_name)
                .select_from(Booking)
                .join(first, true())
                .outerjoin(Professional, Professional.id == Booking.professional_id)
                .where(*where)
                .order_by(first.c.completed_at.desc().nulls_last(), Booking.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        ).all()

        bookings = [
            {
                "booking_id": b.id,
                "completed_at": completed_at,
                "service_amount_paise": to_paise(b.service_amount),
                "discount_amount_paise": to_paise(b.discount_amount),
                "platform_commission_paise": to_paise(b.platform_commission),
                "provider_payout_paise": to_paise(b.provider_payout),
                "tip_amount_paise": to_paise(b.tip_amount),
                "settlement_status": b.settlement_status.value,
                "settlement_payout_id": b.settlement_payout_id,
                "settlement_failure_reason": b.settlement_failure_reason,
                "professional_name": pro_name,
            }
            for b, completed_at, pro_name in rows
        ]
        return {"bookings": bookings, "total": total, "page": page, "per_page": per_page}

    async def export_csv(self, start: date, end: date) -> str:
        """Payout-scope bookings completed on IST days ``start``..``end`` inclusive.

        Gross per booking: ``sp_payout`` is provider_payout + tip, what that booking
        adds to a batch. A range's sum need not equal what RazorpayX sent, since the
        wire amount is the wallet (cash commission netted, T+1 credits only).
        A booking with no COMPLETED status-log row has no completion day, so it is
        left out.
        """
        first = ProviderService._first_completed()
        lo = datetime.combine(start, time.min, tzinfo=_IST)
        hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=_IST)
        rows = (
            await self.db.execute(
                select(
                    Booking.provider_id,
                    func.coalesce(func.nullif(ProviderProfile.business_name, ""), User.name),
                    Booking.id,
                    Booking.service_amount,
                    Booking.platform_commission,
                    Booking.provider_payout + Booking.tip_amount,
                    first.c.completed_at,
                    Booking.settlement_status,
                )
                .select_from(Booking)
                .join(first, true())
                .join(User, User.id == Booking.provider_id)
                .outerjoin(ProviderProfile, ProviderProfile.user_id == Booking.provider_id)
                .where(
                    *PayoutService._payout_scope_filter(),
                    first.c.completed_at >= lo,
                    first.c.completed_at < hi,
                )
                .order_by(first.c.completed_at, Booking.id)
            )
        ).all()

        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(CSV_COLUMNS)
        for sp_id, sp_name, booking_id, price, commission, sp_payout, completed_at, status in rows:
            cells = (
                str(sp_id),
                sp_name or "",
                str(booking_id),
                _rupees(price),
                _rupees(commission),
                _rupees(sp_payout),
                completed_at.astimezone(_IST).isoformat(timespec="seconds"),
                status.value,
            )
            writer.writerow([_csv_cell(c) for c in cells])
        return out.getvalue()
