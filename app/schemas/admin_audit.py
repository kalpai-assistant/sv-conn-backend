from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class AdminAuditRow(BaseModel):
    id: UUID
    admin_user_id: UUID
    admin_name: str | None
    admin_email: str | None
    action: str
    target_type: str
    target_id: str
    sp_user_id: UUID | None
    to_sp_user_id: UUID | None
    reason: str | None
    details: dict[str, Any]
    before_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    ip_address: str | None
    user_agent: str | None
    created_at: datetime


class AdminAuditListResponse(BaseModel):
    items: list[AdminAuditRow]
    total: int
    page: int
    per_page: int


class AdminAuditAdmin(BaseModel):
    id: UUID
    name: str | None
    email: str | None


class AdminAuditFilters(BaseModel):
    admins: list[AdminAuditAdmin]
    actions: list[str]
    target_types: list[str]
