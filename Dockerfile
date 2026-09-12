FROM python:3.11-slim

WORKDIR /app

# System deps (psycopg2 needs libpq, healthcheck needs curl)
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run migrations then start the server.
# PORT is injected by Koyeb / Cloud Run / Render — defaults to 8000.
# Single worker keeps RSS well within the 512 MB free-tier ceiling.
CMD ["sh", "-c", "alembic upgrade head && uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-reload"]