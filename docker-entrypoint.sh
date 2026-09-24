#!/bin/sh
# Container entrypoint — materialises base64-encoded secrets from env
# vars into the on-disk files the app expects, then execs the CMD.
#
# The alternative is teaching every consumer of an on-disk secret to
# decode-and-cache from the env var. Doing it once here keeps app code
# unchanged from the local-dev pattern (`FCM_CREDENTIALS_PATH=/path/to/file`).
#
# Runs as PID 1's setup phase → `exec "$@"` hands off cleanly so signals
# (SIGTERM from App Runner on redeploy) reach uvicorn.
set -eu

# Boot-time identity line — grep for "git_sha=" in CloudWatch to answer
# "which commit is this container on?" without shelling in.
echo "entrypoint: git_sha=${GIT_SHA:-unknown} startup_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# FCM: SSM stores the service-account JSON base64-encoded because SSM
# SecureString values are single-line. Decode into a tmpfs-backed path
# App Runner cleans up on container stop.
if [ -n "${FCM_CREDENTIALS_JSON:-}" ]; then
    FCM_PATH="${FCM_CREDENTIALS_PATH:-/tmp/fcm-credentials.json}"
    echo "$FCM_CREDENTIALS_JSON" | base64 -d > "$FCM_PATH"
    chmod 600 "$FCM_PATH"
    export FCM_CREDENTIALS_PATH="$FCM_PATH"
    echo "entrypoint: FCM credentials written to $FCM_PATH ($(stat -c%s "$FCM_PATH") bytes)"
else
    echo "entrypoint: FCM_CREDENTIALS_JSON not set — running in log-only push mode"
fi

# SVC-305: opt-in in-container alembic upgrade. deploy-beta.yml sets
# RUN_MIGRATIONS_ON_BOOT=1 on the backend App Runner service only for the
# deploy cycle that carries the db-migrate label, and clears it after. The
# worker service never has it set, so this branch is a no-op there.
#
# We run alembic here rather than from the GitHub-hosted runner because
# after SVC-305 lands, RDS is only reachable from inside the VPC — the
# runner can't reach it. The App Runner container is inside the VPC by
# construction.
#
# Failure here exits non-zero, App Runner marks the deploy failed and
# rolls back — same "failed migration stops the deploy" invariant the
# runner-side step provided.
#
# Concurrent instances on a rolling deploy: alembic locks alembic_version
# during the upgrade transaction, so parallel runs serialise; the loser
# sees head and exits 0.
if [ "${RUN_MIGRATIONS_ON_BOOT:-0}" = "1" ]; then
    echo "entrypoint: alembic upgrade head — starting"
    alembic upgrade head
    echo "entrypoint: alembic upgrade complete"
fi

exec "$@"
