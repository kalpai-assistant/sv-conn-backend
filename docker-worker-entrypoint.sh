#!/bin/sh
# SVC-228: Celery worker container start command.
#
# App Runner's ImageConfiguration.StartCommand runs this AFTER the
# main Dockerfile ENTRYPOINT (docker-entrypoint.sh) has materialised
# FCM credentials — so we don't repeat that step here. The image
# entrypoint's `exec "$@"` hands off to this script.
#
# App Runner requires an HTTP port even for background workers (its
# health probe hits /health, failing probes restart the service). We
# run BOTH the celery worker + beat AND a tiny uvicorn health server
# (app/worker_health.py) sharing one process tree.
#
# If either child exits, `wait` returns and the container dies —
# App Runner rolls a fresh instance. Better than one child dying
# silently while the other keeps running on a half-broken service.

set -eu

# Tiny health app — serves ONLY /health so App Runner's probe passes.
# Explicit --host 0.0.0.0 because App Runner routes to whatever the
# container binds to.
echo "worker: starting health server on :8000"
uvicorn app.worker_health:worker_app --host 0.0.0.0 --port 8000 &

# Celery worker + beat scheduler in the SAME process (single-node beat
# is fine at beta scale — the fancy multi-node coordination is only
# worth it past ~10 workers).
#
# Queues are the three routes defined in celery_app.py:
#   default        cleanup tasks + anything unrouted
#   notifications  booking + update-available FCM pushes
#   payments       payout dispatch + reconciliation
echo "worker: starting celery worker + beat"
celery -A app.tasks.celery_app worker \
    --beat \
    --loglevel=info \
    --queues=default,notifications,payments \
    --concurrency=2 &

# Block on all children; return code follows whichever exits.
# `wait -n` (return on first exit) would be nicer but busybox/dash's
# `wait` lacks -n. Trade-off: if celery crashes but uvicorn stays
# alive, the container hangs. Acceptable at beta scale; a proper
# init like tini is the follow-up (SVC-228.1).
wait
