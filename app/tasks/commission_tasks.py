"""SVC-486: the commission-change sweep and the notices to SPs.

* ``apply_commission_changes`` (beat, every 60 s) applies every scheduled change
  whose date has come, through ``apply_due_commission_changes``, the one place
  that does it. A booking on the service also applies it, before reading the
  variant, so the sweep only decides how soon the SP pricing screen and the
  admin editor catch up.
* ``send_commission_change_notices`` tells every SP with an active price on the
  variant, through each channel, and writes one ``commission_change_notices``
  row per SP per channel. That row is the SP's paper trail (Vipul, SVC-486
  comment 14963). It is enqueued after the admin's save commits.

Recipients: an ACTIVE ``sp_service_pricing`` row on the variant, an active
account, the provider role. Prices are per SP, so a team Pro never gets one,
and a Pro working for themselves is the SP. ``sp_service_pricing`` is unique
per (SP, variant), so each SP comes up once.

Channels, each recorded with its outcome:

* push in the SP's language: ``sent``, ``failed``, ``skipped_no_token``, or
  ``mocked`` while FCM is unconfigured outside production;
* email: ``sent``, ``failed``, ``skipped_no_email`` (option B: no email means
  push only), or ``mocked`` while ``EMAIL_FROM`` is unset;
* SMS, for a scheduled change only, since there is no template for a
  cancellation: ``sent``, ``failed``, or ``mocked`` while
  ``MSG91_COMMISSION_TEMPLATE_ID`` is unset.

An (SP, channel) pair that already has a row for this change and kind is
skipped, so a second run sends nothing twice. Rows are written after each
channel. The task retries only its first reads, before anything is sent.
"""

import asyncio
import html
import logging
import uuid
from dataclasses import dataclass

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.commission_tasks.apply_commission_changes")
def apply_commission_changes() -> int:
    """Apply every commission change that is due. Returns how many applied.

    One transaction per service, so the sweep never holds two services' locks
    at once. A multi-service booking inserting its items takes key-share locks
    on its services in cart order, so a sweep holding several could deadlock
    with it. A service that fails is logged, and the next run retries it.
    """
    from app.database import AsyncSessionLocal
    from app.services.commission_changes import apply_due_commission_changes, due_service_ids

    async def _run() -> list[uuid.UUID]:
        async with AsyncSessionLocal() as session:
            service_ids = await due_service_ids(session)
        applied: list[uuid.UUID] = []
        for service_id in service_ids:
            async with AsyncSessionLocal() as session:
                try:
                    applied += await apply_due_commission_changes(session, service_ids=[service_id])
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.exception("commission_change.apply_failed service_id=%s", service_id)
        return applied

    applied = asyncio.run(_run())
    if applied:
        logger.info(
            "commission_change.applied count=%d change_ids=%s",
            len(applied),
            ",".join(str(i) for i in applied),
        )
    return len(applied)


@dataclass(frozen=True)
class _Notice:
    service_id: str
    variant_id: str
    service: str
    label: str
    old_pct: str
    new_pct: str
    date: str
    push_key: str


@dataclass(frozen=True)
class _Recipient:
    """One SP. ``phone`` and ``preferred_language`` are what ``push_language``
    reads."""

    id: uuid.UUID
    fcm_token: str | None
    preferred_language: str | None
    phone: str
    email: str | None


def _email_copy(notice: _Notice, kind: str) -> tuple[str, str, str]:
    """``(subject, text, html)`` for one notice email. English only, like the
    SMS template."""
    from app.models.commission_change import KIND_CANCELLED

    option = f"{notice.service} - {notice.label}"
    if kind == KIND_CANCELLED:
        subject = f"Commission change cancelled: {option}"
        lines = [
            f"The commission change on {option} has been cancelled.",
            f"It would have changed the platform commission from {notice.old_pct}% "
            f"to {notice.new_pct}% from {notice.date}.",
            f"Your commission stays at {notice.old_pct}%.",
        ]
    else:
        subject = f"Commission change: {option}"
        lines = [
            f"Service: {notice.service}",
            f"Option: {notice.label}",
            f"Current commission: {notice.old_pct}%",
            f"New commission: {notice.new_pct}%",
            f"Takes effect: {notice.date}",
            "This email is your notice of the change. Your bookings before that date "
            "keep the current commission.",
        ]
    lines.append("Details are in the EazyServ app.")
    text = "\n\n".join(lines) + "\n"
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    page = (
        '<!doctype html><html><body style="font-family:Arial,sans-serif;font-size:15px">'
        f"<h2>{html.escape(subject)}</h2>{body}"
        '<p style="color:#666;font-size:12px">EazyServ. Please don\'t reply to this email.</p>'
        "</body></html>"
    )
    return subject, text, page


def _record(change_id: uuid.UUID, kind: str, channel: str, rows: list[tuple]) -> None:
    """Write one notice row per ``(sp_id, status, error)``. A row that already
    exists is left alone."""
    if not rows:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.database import SyncSessionLocal
    from app.models.commission_change import CommissionChangeNotice

    values = [
        {
            "id": uuid.uuid4(),
            "change_id": change_id,
            "sp_id": sp_id,
            "kind": kind,
            "channel": channel,
            "status": status,
            "error": None if error is None else error[:2000],
        }
        for sp_id, status, error in rows
    ]
    with SyncSessionLocal() as db:
        db.execute(
            pg_insert(CommissionChangeNotice)
            .values(values)
            .on_conflict_do_nothing(constraint="uq_commission_change_notices_once")
        )
        db.commit()


@celery_app.task(
    bind=True,
    # Only the reads below retry: once anything has been sent, a retry would
    # repeat it (the per-row skip makes that harmless, but it costs a run).
    max_retries=1,
    default_retry_delay=30,
    name="app.tasks.commission_tasks.send_commission_change_notices",
)
def send_commission_change_notices(self, change_id: str, kind: str) -> dict:
    """Notify every SP pricing the change's variant (module docstring).
    Returns a count per channel and status."""
    from sqlalchemy import select

    from app.database import SyncSessionLocal
    from app.models.commission_change import (
        CHANNEL_EMAIL,
        CHANNEL_PUSH,
        CHANNEL_SMS,
        KIND_CANCELLED,
        CommissionChange,
        CommissionChangeNotice,
    )
    from app.models.provider_profile import ProviderProfile
    from app.models.service import Service
    from app.models.service_variant import ServiceVariant
    from app.models.sp_service_pricing import SpServicePricing
    from app.models.user import User, UserRole
    from app.services.commission_changes import format_notice_date, format_pct
    from app.services.provider_service import _IST

    cid = uuid.UUID(change_id)
    try:
        with SyncSessionLocal() as db:
            change = db.get(CommissionChange, cid)
            variant = db.get(ServiceVariant, change.service_variant_id) if change else None
            service = db.get(Service, variant.service_id) if variant else None
            if change is None or variant is None or service is None:
                logger.info("commission_change_notices_skipped change_id=%s", change_id)
                return {}
            if kind == KIND_CANCELLED:
                push_key = "commission_change_cancelled"
            elif change.effective_from <= change.created_at.astimezone(_IST).date():
                push_key = "commission_change_today"
            else:
                push_key = "commission_change"
            notice = _Notice(
                service_id=str(service.id),
                variant_id=str(variant.id),
                service=service.name,
                label=variant.label,
                old_pct=format_pct(change.old_pct),
                new_pct=format_pct(change.new_pct),
                date=format_notice_date(change.effective_from),
                push_key=push_key,
            )
            recipients = [
                _Recipient(*row)
                for row in db.execute(
                    select(User.id, User.fcm_token, User.preferred_language, User.phone, User.email)
                    .join(SpServicePricing, SpServicePricing.sp_id == User.id)
                    .join(ProviderProfile, ProviderProfile.user_id == User.id)
                    .where(
                        SpServicePricing.service_variant_id == variant.id,
                        SpServicePricing.is_active.is_(True),
                        User.is_active.is_(True),
                        User.role == UserRole.PROVIDER,
                    )
                    .order_by(User.id)
                ).all()
            ]
            done = set(
                db.execute(
                    select(CommissionChangeNotice.sp_id, CommissionChangeNotice.channel).where(
                        CommissionChangeNotice.change_id == cid,
                        CommissionChangeNotice.kind == kind,
                    )
                )
                .tuples()
                .all()
            )
    except Exception as exc:
        # Nothing has been sent yet.
        raise self.retry(exc=exc) from exc

    summary: dict[str, dict[str, int]] = {}

    def pending(channel: str) -> list[_Recipient]:
        return [r for r in recipients if (r.id, channel) not in done]

    def finish(channel: str, rows: list[tuple]) -> None:
        _record(cid, kind, channel, rows)
        counts = summary.setdefault(channel, {})
        for _, status, _ in rows:
            counts[status] = counts.get(status, 0) + 1

    finish(CHANNEL_PUSH, _send_push(notice, cid, pending(CHANNEL_PUSH)))
    finish(CHANNEL_EMAIL, _send_email(notice, kind, pending(CHANNEL_EMAIL)))
    if kind != KIND_CANCELLED:
        finish(CHANNEL_SMS, _send_sms(notice, pending(CHANNEL_SMS)))

    logger.info(
        "commission_change_notices_sent change_id=%s kind=%s recipients=%d summary=%s",
        change_id,
        kind,
        len(recipients),
        summary,
    )
    return summary


def _send_push(notice: _Notice, change_id: uuid.UUID, recipients: list[_Recipient]) -> list:
    from app.models.commission_change import (
        NOTICE_FAILED,
        NOTICE_MOCKED,
        NOTICE_SENT,
        NOTICE_SKIPPED_NO_TOKEN,
    )
    from app.tasks.notification_tasks import provider_push_copy, push_language, send_multicast

    rows: list[tuple] = []
    by_language: dict[str, list[_Recipient]] = {}
    for r in recipients:
        if not r.fcm_token:
            rows.append((r.id, NOTICE_SKIPPED_NO_TOKEN, None))
        else:
            by_language.setdefault(push_language(r), []).append(r)

    data = {
        "type": "pricing",
        "event": "commission_change",
        "service_id": notice.service_id,
        "variant_id": notice.variant_id,
        "change_id": str(change_id),
    }
    for lang, group in by_language.items():
        title, body = provider_push_copy(
            notice.push_key,
            lang,
            service=notice.service,
            label=notice.label,
            old_pct=notice.old_pct,
            new_pct=notice.new_pct,
            date=notice.date,
        )
        try:
            sent = send_multicast(
                tokens=[r.fcm_token for r in group],
                title=title,
                body=body,
                data=data,
                per_token=True,
            )
        except Exception as exc:
            logger.exception("commission_change_push_failed change_id=%s lang=%s", change_id, lang)
            rows.extend((r.id, NOTICE_FAILED, str(exc)) for r in group)
            continue
        for r, (ok, error) in zip(group, sent["results"], strict=True):
            if not ok:
                rows.append((r.id, NOTICE_FAILED, error))
            else:
                rows.append((r.id, NOTICE_MOCKED if sent["mocked"] else NOTICE_SENT, None))
    return rows


def _send_email(notice: _Notice, kind: str, recipients: list[_Recipient]) -> list:
    from app.models.commission_change import (
        NOTICE_FAILED,
        NOTICE_MOCKED,
        NOTICE_SENT,
        NOTICE_SKIPPED_NO_EMAIL,
    )
    from app.utils.email_client import send_email

    subject, text, page = _email_copy(notice, kind)
    rows: list[tuple] = []
    for r in recipients:
        if not r.email:
            rows.append((r.id, NOTICE_SKIPPED_NO_EMAIL, None))
            continue
        try:
            outcome = send_email(to=r.email, subject=subject, text=text, html=page)
        except Exception as exc:
            logger.warning("commission_change_email_failed sp_id=%s error=%s", r.id, exc)
            rows.append((r.id, NOTICE_FAILED, str(exc)))
            continue
        rows.append((r.id, NOTICE_MOCKED if outcome == "mocked" else NOTICE_SENT, None))
    return rows


def _send_sms(notice: _Notice, recipients: list[_Recipient]) -> list:
    from app.models.commission_change import NOTICE_FAILED, NOTICE_MOCKED, NOTICE_SENT
    from app.utils.sms_client import send_commission_change

    async def _run() -> list[tuple]:
        rows: list[tuple] = []
        for r in recipients:
            try:
                sent = await send_commission_change(
                    r.phone,
                    notice.service,
                    notice.label,
                    notice.old_pct,
                    notice.new_pct,
                    notice.date,
                )
            except Exception as exc:
                logger.warning("commission_change_sms_failed sp_id=%s error=%s", r.id, exc)
                rows.append((r.id, NOTICE_FAILED, str(exc)))
                continue
            rows.append((r.id, NOTICE_SENT if sent else NOTICE_MOCKED, None))
        return rows

    if not recipients:
        return []
    return asyncio.run(_run())
