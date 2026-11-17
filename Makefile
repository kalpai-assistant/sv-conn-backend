# EazyServ backend — developer targets.
#
# The only target intended for routine use is ``make stability-check`` (SVC-456).
# Everything else here is a convenience wrapper around commands already
# documented in CLAUDE.md so a dev with muscle memory can keep using uv run
# directly without this Makefile getting in the way.

.PHONY: help stability-check stability-up stability-down test lint format

help:
	@echo "EazyServ backend — common targets"
	@echo ""
	@echo "  make stability-check  Pre-push gate (SVC-456): fast pytest + migration"
	@echo "                        round-trip + webhook contract tests + guards."
	@echo "                        Target runtime: <5 min against docker-compose Postgres."
	@echo "  make stability-up     Boot docker-compose Postgres + Redis for the check."
	@echo "  make stability-down   Tear them down."
	@echo "  make test             Full pytest suite."
	@echo "  make lint             Ruff check."
	@echo "  make format           Ruff format."

# --- Stability wave 1 gate (SVC-456) ---
#
# Runs the stability net pre-push in <5 min on a laptop. Assumes the
# docker-compose Postgres + Redis are up (use ``make stability-up`` if not).
# No new paid services; GitHub Actions CI already runs the full suite.
#
# Composition (order chosen by expected-failure-cost ÷ wall-time):
#
# 1. ``alembic check``                     migration drift against models   (~2 s)
# 2. ``pytest -m stability``               the opt-in longer-running tests  (~90 s)
#    - SVC-457 full-chain migration round-trip (G1)
#    - any future @pytest.mark.stability-tagged tests
# 3. ``pytest -m "not stability"``         everything else — 655 tests      (~2:45)
#
# Total budget: ~5 min, which matches the pre-push time-to-signal requirement
# in kalpai-run-01-stability.md §Deliverables (4).
#
# Webhook contract tests (SVC-458 / 459 / 460) run under the ``not stability``
# bucket once their captured-payload fixtures land; the harness lives in
# tests/test_webhook_contracts.py (added by those PRs). Until then, this
# target is self-consistent — it just doesn't exercise contracts yet.

stability-check:
	@echo ""
	@echo "=== SVC-456 stability-check ==="
	@echo "Expected runtime: <5 min. Pre-push gate."
	@echo ""
	@echo "[1/3] alembic check — migration drift"
	uv run alembic check
	@echo ""
	@echo "[2/3] pytest -m stability — long-running regression tests"
	uv run pytest -m stability --tb=short -q
	@echo ""
	@echo "[3/3] pytest -m 'not stability' — the fast suite"
	uv run pytest -m "not stability" --tb=short -q
	@echo ""
	@echo "=== stability-check green ==="

stability-up:
	docker-compose up -d postgres redis
	@echo "Waiting for Postgres to accept connections…"
	@until docker-compose exec -T postgres pg_isready -U sc_user -d eazyserv > /dev/null 2>&1; do sleep 1; done
	@echo "Creating sc_test DB (idempotent) + PostGIS extension…"
	@docker-compose exec -T postgres psql -U sc_user -d eazyserv -c "CREATE DATABASE sc_test;" 2>/dev/null || true
	@docker-compose exec -T postgres psql -U sc_user -d sc_test -c "CREATE EXTENSION IF NOT EXISTS postgis;"
	@echo "Ready. TEST_DATABASE_URL=postgresql+asyncpg://sc_user:sc_password@localhost:5432/sc_test"

stability-down:
	docker-compose stop postgres redis

# --- Convenience wrappers around CLAUDE.md commands ---

test:
	uv run pytest tests/ -v

lint:
	uv run ruff check .

format:
	uv run ruff format .
