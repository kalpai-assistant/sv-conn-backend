"""
Admin IP allowlist middleware (SVC-75).

Gates `/admin/*` on top of the existing role check. Even a leaked admin JWT is
useless from an unlisted network. Only enforces when configured — an empty
allowlist means "no restriction", so dev environments keep working out of the
box and production must set the env var explicitly.

Config:
- ADMIN_ALLOWED_IPS: comma-separated CIDRs (e.g. "203.0.113.4/32,10.0.0.0/8")
- ADMIN_TRUST_FORWARDED_FOR: when the API sits behind a load balancer/CDN
  (ALB, Cloudflare), the client IP arrives in X-Forwarded-For. Trust it only
  when explicitly opted in — trusting it unconditionally lets any client spoof
  their IP via a header.
"""

import ipaddress
import logging

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


def _parse_allowlist(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """
    Parse "10.0.0.0/8, 203.0.113.4/32" into a list of ip_network objects.

    Bare IPs (no /prefix) are treated as /32 (IPv4) or /128 (IPv6). Malformed
    entries are logged and dropped rather than crashing startup — a typo
    shouldn't take the API down.
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            logger.error("Ignoring malformed ADMIN_ALLOWED_IPS entry %r: %s", entry, exc)
    return networks


def _client_ip(request: Request, trust_forwarded: bool) -> str | None:
    """
    Pull the client IP. Behind an ALB/CDN, `request.client.host` is the LB —
    the real client sits in X-Forwarded-For's leftmost entry. We only honour
    that header when explicitly told to; otherwise anyone can spoof it.
    """
    if trust_forwarded:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # X-Forwarded-For: client, proxy1, proxy2 → left-most is the client
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else None


class AdminIPAllowlistMiddleware(BaseHTTPMiddleware):
    """
    Enforce an IP allowlist on `/admin/*` when configured.

    No-op if `allowed` is empty — keeps dev / local runs working without
    forcing config. When the allowlist is set, any admin request from an
    unlisted IP returns 403 before the router (and therefore before the
    role check) ever runs. This layering makes the block cheap and means
    even a stolen JWT is inert from the wrong network.
    """

    _PROTECTED_PREFIX = "/admin"

    def __init__(
        self,
        app,
        allowed_ips: str = "",
        trust_forwarded_for: bool = False,
        api_prefix: str = "",
    ):
        super().__init__(app)
        self._networks = _parse_allowlist(allowed_ips)
        self._trust_forwarded = trust_forwarded_for
        # The API is mounted under a prefix (e.g. /api/v1); admin routes live
        # under `{prefix}/admin/*`. We match against the raw request path.
        self._protected_path = f"{api_prefix.rstrip('/')}{self._PROTECTED_PREFIX}"

        if self._networks:
            logger.info(
                "Admin IP allowlist active: %d network(s), trust_forwarded_for=%s",
                len(self._networks),
                self._trust_forwarded,
            )
        else:
            logger.info(
                "Admin IP allowlist empty — no IP restriction on %s (dev mode)",
                self._protected_path,
            )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Non-admin paths bypass the check entirely. Docs/health/etc. never hit
        # this branch either.
        if not path.startswith(self._protected_path):
            return await call_next(request)

        # Empty allowlist = allow all — dev-safe default. Production MUST
        # configure ADMIN_ALLOWED_IPS; enforcement of that lives in the deploy
        # checklist, not here (a boot-time crash would take down non-admin
        # traffic too, which is worse than the silent-open state).
        if not self._networks:
            return await call_next(request)

        raw_ip = _client_ip(request, self._trust_forwarded)
        try:
            addr = ipaddress.ip_address(raw_ip) if raw_ip else None
        except ValueError:
            addr = None

        if addr is None or not any(addr in net for net in self._networks):
            # Deliberately generic message — never leak which IPs are allowed.
            # Log the offender so ops can whitelist a genuine admin whose home
            # IP changed.
            logger.warning(
                "Admin route rejected: ip=%s path=%s user_agent=%s",
                raw_ip or "unknown",
                path,
                request.headers.get("user-agent", "unknown"),
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": True,
                    "message": "Admin access is restricted from this network.",
                    "status_code": 403,
                },
            )

        return await call_next(request)
