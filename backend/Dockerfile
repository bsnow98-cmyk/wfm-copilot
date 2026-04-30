# Python 3.12 slim — small image, modern enough for everything we need.
FROM python:3.12-slim

# System deps:
#   build-essential — for any package that compiles C extensions
#   libpq-dev       — for psycopg (Postgres driver)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better Docker layer caching).
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Then copy the app code. In dev, docker-compose mounts ./backend over /app
# which makes this COPY effectively a no-op at runtime, but it lets `docker build`
# produce a self-contained image for prod.
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
