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

exec "$@"
