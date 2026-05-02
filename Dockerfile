FROM python:3.11-slim

WORKDIR /code

# Install system deps for psycopg2 + geoalchemy2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock* ./

# Install uv and sync dependencies
RUN pip install --no-cache-dir uv && uv sync --no-dev --frozen

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
