"""Response schemas for the downloads endpoints (SVC-161, SVC-162)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PlatformVersion(BaseModel):
    """Version metadata for one platform (android / pwa).

    ``version`` and ``min_supported_version`` are SemVer strings. Clients
    compare using SemVer semantics — a lex compare would treat 1.10 < 1.2.
    """

    model_config = ConfigDict(from_attributes=True)

    version: str
    min_supported_version: str
    url: str | None
    release_notes: str | None
    released_at: datetime


class LatestVersionResponse(BaseModel):
    """The whole payload returned by GET /downloads/latest.json.

    Shape is stable — the frontend hook branches on the presence of each
    platform key. Adding a new platform (post-beta iOS native) means
    adding a new required key here + a matching row in ``app_versions``.
    """

    android: PlatformVersion
    pwa: PlatformVersion
