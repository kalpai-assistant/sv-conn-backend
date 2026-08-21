"""Tiny FastAPI app for the Celery worker container's health endpoint (SVC-228).

App Runner requires an HTTP port even for background workloads — its
health-check probe hits ``GET /health``, and a failing probe restarts
the service. The Celery worker itself doesn't serve HTTP, so this
module provides the bare minimum: a single 200 endpoint the load
balancer can hit.

The worker container's start command runs BOTH uvicorn (serving this
app) and celery worker + beat, sharing the same process tree. See
``docker-worker-entrypoint.sh``. If either process dies, the container
exits and App Runner rolls a new instance.

Deliberately NOT importing the main FastAPI app — that would drag in
routers, middleware, Sentry init, all the request-handling machinery
the worker doesn't need. This module is 20 lines by design.
"""

from fastapi import FastAPI

worker_app = FastAPI(
    title="EazyServ worker health",
    docs_url=None,  # No docs on a background service.
    redoc_url=None,
    openapi_url=None,
)


@worker_app.get("/health")
def health() -> dict[str, str]:
    """Load-balancer health check. Doesn't touch Redis or Postgres —
    a dead Redis would fail every actual task but the LB probe should
    not restart the container for that reason (Celery has its own retry
    behaviour, and thrashing the worker on transient Redis blips makes
    the outage worse)."""
    return {"status": "ok"}
