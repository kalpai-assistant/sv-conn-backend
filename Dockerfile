# App Runner-ready image. Slim Python 3.11 + uv, with the venv on PATH so
# uvicorn is directly callable at startup — otherwise the CMD would need
# `uv run uvicorn` and take an extra second per container start.

FROM python:3.11-slim

WORKDIR /code

# System deps: libpq for psycopg2 (Celery sync path), gcc + build tools for
# anything that needs compilation from the wheel misses (cryptography, etc.
# ship prebuilt wheels for slim so this is mostly harmless).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# uv installs to a project-local .venv. Put it first on PATH so uvicorn,
# alembic, etc. resolve without a shim.
ENV PATH="/code/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/code/.venv

# Dependency install first, before app source, so a code-only change
# doesn't invalidate this layer.
COPY pyproject.toml uv.lock* ./
RUN pip install --no-cache-dir uv && uv sync --no-dev --frozen

# App source.
COPY . .

# Entrypoint materialises any base64-encoded secrets from SSM-injected env
# vars into on-disk files the app expects (currently just FCM_CREDENTIALS_JSON
# → FCM_CREDENTIALS_PATH). See docker-entrypoint.sh comments.
RUN chmod +x /code/docker-entrypoint.sh
ENTRYPOINT ["/code/docker-entrypoint.sh"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
