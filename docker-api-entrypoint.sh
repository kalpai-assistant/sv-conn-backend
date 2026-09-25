#!/bin/sh
# SVC-305: API-container startup wrapper.
#
# Runs `alembic upgrade head` unconditionally, then execs "$@" (uvicorn).
# Idempotent — alembic is a no-op when the schema is already at HEAD, so
# it is safe to run on every API container boot. The fast path is ~1 s.
#
# This script is the CMD for the API service ONLY. The Celery worker's
# App Runner service overrides CMD via ImageConfiguration.StartCommand to
# /code/docker-worker-entrypoint.sh, which never calls this script — see
# the header of that file for the "why the worker doesn't migrate" story.
#
# Rollback story: if alembic exits non-zero, this script exits non-zero,
# App Runner marks the deploy failed and auto-rolls back to the previous
# image. Beta keeps serving the old code against its own schema — same
# "failed migration stops the deploy" invariant the old GitHub-hosted
# runner alembic step provided, now enforced from inside the VPC where
# the runner cannot reach beta RDS anymore.
#
# Concurrency: on a rolling deploy the new container boots before the old
# drains. If instance count is ever raised above 1, parallel
# `alembic upgrade head` calls serialise on alembic's own version-table
# lock; whichever loses observes HEAD and exits 0.

set -eu

echo "entrypoint(api): alembic upgrade head — before=$(alembic current 2>&1 | tail -1)"
alembic upgrade head
echo "entrypoint(api): alembic upgrade complete — after=$(alembic current 2>&1 | tail -1)"

exec "$@"
