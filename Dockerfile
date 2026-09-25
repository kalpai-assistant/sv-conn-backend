# App Runner-ready image. Slim Python 3.11 + uv, with the venv on PATH so
# uvicorn is directly callable at startup — otherwise the CMD would need
# `uv run uvicorn` and take an extra second per container start.

FROM python:3.11-slim

WORKDIR /code

# System deps:
# - libpq-dev: psycopg2 (Celery sync DB path)
# - libcurl4-openssl-dev + libssl-dev: pycurl, which kombu 5.3/5.4 requires
#   for its async SQS transport. Pycurl has no prebuilt wheel for slim, so
#   these headers + gcc are mandatory — dropping any of them breaks the
#   worker at runtime, not at build time (see PR #136 follow-up).
# - gcc: build tool for anything else that misses a prebuilt wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev libcurl4-openssl-dev libssl-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# uv installs to a project-local .venv. Put it first on PATH so uvicorn,
# alembic, etc. resolve without a shim.
ENV PATH="/code/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/code/.venv

# Baked at build time from the CI workflow (`--build-arg GIT_SHA=$GITHUB_SHA`).
# Surfaces at boot via the entrypoints so `aws logs tail ... | grep git_sha`
# tells you exactly which commit a running container is on — no more
# "is the worker on the new image?" guessing.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

# Dependency install first, before app source, so a code-only change
# doesn't invalidate this layer.
COPY pyproject.toml uv.lock* ./
RUN pip install --no-cache-dir uv && uv sync --no-dev --frozen

# App source.
COPY . .

# Entrypoints:
#  - docker-entrypoint.sh (ENTRYPOINT): materialises base64-encoded secrets
#    from SSM-injected env vars into on-disk files (currently just
#    FCM_CREDENTIALS_JSON → FCM_CREDENTIALS_PATH), then execs $@.
#  - docker-api-entrypoint.sh (default CMD wrapper, SVC-305): runs
#    `alembic upgrade head` on the API container's boot, then execs uvicorn.
#  - docker-worker-entrypoint.sh: CMD override the Celery worker App Runner
#    service passes as ImageConfiguration.StartCommand (SVC-228). Does NOT
#    run alembic — see its header for the rationale.
# Chmod all three so any can be exec'd from the outer entrypoint.
RUN chmod +x /code/docker-entrypoint.sh /code/docker-api-entrypoint.sh /code/docker-worker-entrypoint.sh
ENTRYPOINT ["/code/docker-entrypoint.sh"]

EXPOSE 8000
CMD ["/code/docker-api-entrypoint.sh", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
