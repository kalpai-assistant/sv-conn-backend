"""
The admin audit log (SVC-423): every admin write writes one ``admin_actions`` row.

Calls are explicit in each write handler, in the same transaction as the change,
so a code review can see which actions are audited and a rollback takes the row
with it::

    before = snapshot("service", service)
    ...change it...
    await audit_admin_action(
        db,
        audit_actor(request, admin),
        action="service.update",
        target_type="service",
        target_id=service.id,
        before=before,
        after=snapshot("service", service),
    )

``before`` and ``after`` hold short allow-lists per record type, never a whole
record. New action names go in ``app/models/admin_action.py``.
"""

import enum
import ipaddress
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_action import USER_AGENT_MAX_LENGTH, AdminAction
from app.models.user import User
from app.schemas.admin_audit import (
    AdminAuditAdmin,
    AdminAuditFilters,
    AdminAuditListResponse,
    AdminAuditRow,
)
from app.utils.rate_limiter import appended_client_ip

# What each record type may show in before_json / after_json. Add to these with
# care: tests/test_admin_audit.py fails if a blocked field is ever listed.
AUDIT_FIELDS: dict[str, tuple[str, ...]] = {
    "user": ("is_active",),
    "provider_profile": ("kyc_status", "kyc_reviewed_at", "kyc_rejection_reason"),
    "professional": (
        "id",
        "service_provider_id",
        "user_id",
        "is_active",
        "is_online",
        "kyc_status",
        "kyc_reviewed_at",
        "kyc_rejection_reason",
    ),
    "booking": (
        "status",
        "payment_status",
        "settlement_status",
        "cancellation_reason",
        "provider_id",
        "professional_id",
    ),
    "app_version": ("version", "min_supported_version", "release_notes", "url", "released_at"),
    "service": (
        "category_id",
        "name",
        "description",
        "base_price",
        "duration_minutes",
        "is_active",
    ),
    # SVC-428: a pricing save records only the fields it changed, from these.
    "category": (
        "is_sensitive",
        "reference_price",
        "bounds_pct",
        "platform_commission_pct",
        "platform_commission_min",
        "commission_gst_pct",
    ),
    "pricing_config": ("value_json",),
    # SVC-429: a create records all of these, an update only the ones it changed.
    "coupon": (
        "code",
        "discount_type",
        "discount_value",
        "max_discount",
        "min_order_amount",
        "valid_from",
        "valid_until",
        "usage_limit",
        "usage_limit_per_user",
        "is_active",
        "user_id",
    ),
}

# Never in an audit row, whatever a caller passes. So is every ``*_encrypted``
# column, which is matched by suffix so a new one is covered without an edit here.
BLOCKED_FIELDS = frozenset(
    {
        "password_hash",
        "totp_secret",
        "fcm_token",
        "razorpayx_contact_id",
        "razorpayx_fund_account_id",
        "kyc_docs",
        "kyc_ai_verdict",
        "doorstep_otp",
    }
)


def is_blocked_field(name: str) -> bool:
    return name in BLOCKED_FIELDS or name.endswith("_encrypted")


def _audit_safe(value: Any) -> Any:
    """Make a value JSON-safe, dropping blocked keys at any depth.

    Decimal becomes float, the form money takes in API responses, so a service
    created with a float and later read back as a Decimal looks the same.
    """
    if isinstance(value, dict):
        return {str(k): _audit_safe(v) for k, v in value.items() if not is_blocked_field(str(k))}
    if isinstance(value, list | tuple | set | frozenset):
        return [_audit_safe(v) for v in value]
    # Before the str check: most enums here are str subclasses.
    if isinstance(value, enum.Enum):
        return value.value
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def snapshot(record_type: str, row: Any) -> dict | None:
    """The allow-listed fields of ``row`` as JSON-safe values. None for no row."""
    if row is None:
        return None
    return _audit_safe({field: getattr(row, field) for field in AUDIT_FIELDS[record_type]})


@dataclass(frozen=True)
class AuditActor:
    """Who is acting, and from where."""

    admin_id: uuid.UUID
    ip_address: str | None = None
    user_agent: str | None = None


def _safe_ip(request: Request) -> str | None:
    """The caller's IP, or None: recording it must never fail the real action.

    ``appended_client_ip`` is the login limit's helper, because a client can't
    choose the rightmost X-Forwarded-For entry. It can return "unknown" or
    whatever a proxy wrote. Python accepts a scoped address (``fe80::1%eth0``)
    that Postgres inet refuses, so that saves as None too.
    """
    try:
        parsed = ipaddress.ip_address(appended_client_ip(request))
    except ValueError:
        return None
    if getattr(parsed, "scope_id", None):
        return None
    return str(parsed)


def _safe_user_agent(request: Request) -> str | None:
    # Postgres text can't hold a NUL, which would fail the transaction.
    raw = request.headers.get("user-agent", "").replace("\x00", "")
    return raw[:USER_AGENT_MAX_LENGTH] or None


def audit_actor(request: Request, admin: User) -> AuditActor:
    return AuditActor(
        admin_id=admin.id, ip_address=_safe_ip(request), user_agent=_safe_user_agent(request)
    )


async def record_admin_action(
    db: AsyncSession,
    *,
    admin_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: uuid.UUID | str,
    reason: str | None,
    details: dict,
    sp_user_id: uuid.UUID | None = None,
    to_sp_user_id: uuid.UUID | None = None,
    before_json: dict | None = None,
    after_json: dict | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AdminAction:
    """Append one ``admin_actions`` row: the only writer. Call
    ``audit_admin_action`` rather than this."""
    row = AdminAction(
        admin_user_id=admin_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        sp_user_id=sp_user_id,
        to_sp_user_id=to_sp_user_id,
        reason=reason,
        details=details,
        before_json=before_json,
        after_json=after_json,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(row)
    await db.flush()
    return row


async def audit_admin_action(
    db: AsyncSession,
    actor: AuditActor,
    *,
    action: str,
    target_type: str,
    target_id: uuid.UUID | str,
    reason: str | None = None,
    details: dict | None = None,
    before: dict | None = None,
    after: dict | None = None,
    sp_user_id: uuid.UUID | None = None,
    to_sp_user_id: uuid.UUID | None = None,
) -> AdminAction:
    """Audit one admin write. Call it in the same transaction as the change.

    ``before`` is None for a create and ``after`` is None for a delete. Build
    them with ``snapshot``.
    """
    return await record_admin_action(
        db,
        admin_id=actor.admin_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        details=_audit_safe(details or {}),
        sp_user_id=sp_user_id,
        to_sp_user_id=to_sp_user_id,
        before_json=_audit_safe(before),
        after_json=_audit_safe(after),
        ip_address=actor.ip_address,
        user_agent=actor.user_agent,
    )


def _as_utc(moment: datetime) -> datetime:
    """A datetime with no offset means UTC. Comparing a naive one with a
    timestamptz column would be a 500."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class AdminAuditService:
    """Reads the log for GET /admin/audit. Writes never go through here."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_rows(
        self,
        *,
        admin_user_id: uuid.UUID | None,
        action: str | None,
        target_type: str | None,
        target_id: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        page: int,
        per_page: int,
    ) -> AdminAuditListResponse:
        """Newest first. ``created_at >= date_from`` and ``< date_to``."""
        where = []
        if admin_user_id is not None:
            where.append(AdminAction.admin_user_id == admin_user_id)
        if action is not None:
            where.append(AdminAction.action == action)
        if target_type is not None:
            where.append(AdminAction.target_type == target_type)
        if target_id is not None:
            where.append(AdminAction.target_id == target_id)
        if date_from is not None:
            where.append(AdminAction.created_at >= _as_utc(date_from))
        if date_to is not None:
            where.append(AdminAction.created_at < _as_utc(date_to))

        total = await self.db.scalar(select(func.count()).select_from(AdminAction).where(*where))
        rows = await self.db.execute(
            select(AdminAction, User.name, User.email)
            .join(User, User.id == AdminAction.admin_user_id)
            .where(*where)
            # The id breaks ties, so a row can't jump between pages.
            .order_by(AdminAction.created_at.desc(), AdminAction.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        return AdminAuditListResponse(
            items=[
                AdminAuditRow(
                    id=row.id,
                    admin_user_id=row.admin_user_id,
                    admin_name=name,
                    admin_email=email,
                    action=row.action,
                    target_type=row.target_type,
                    target_id=row.target_id,
                    sp_user_id=row.sp_user_id,
                    to_sp_user_id=row.to_sp_user_id,
                    reason=row.reason,
                    details=row.details,
                    before_json=row.before_json,
                    after_json=row.after_json,
                    ip_address=str(row.ip_address) if row.ip_address is not None else None,
                    user_agent=row.user_agent,
                    created_at=row.created_at,
                )
                for row, name, email in rows.all()
            ],
            total=total,
            page=page,
            per_page=per_page,
        )

    async def filters(self) -> AdminAuditFilters:
        """The admins, actions and target types present in the log."""
        admins = await self.db.execute(
            select(User.id, User.name, User.email)
            .where(User.id.in_(select(AdminAction.admin_user_id).distinct()))
            .order_by(User.name, User.id)
        )
        actions = await self.db.scalars(
            select(AdminAction.action).distinct().order_by(AdminAction.action)
        )
        target_types = await self.db.scalars(
            select(AdminAction.target_type).distinct().order_by(AdminAction.target_type)
        )
        return AdminAuditFilters(
            admins=[AdminAuditAdmin(id=i, name=name, email=email) for i, name, email in admins],
            actions=list(actions),
            target_types=list(target_types),
        )
