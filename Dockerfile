# Multi-stage build: dependencies are resolved once and the runtime image
# carries no build toolchain and no root shell.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY procureguard ./procureguard

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install ".[documents]"


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=prod

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY procureguard ./procureguard
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# Local object-store and outbox paths, writable by the unprivileged user. In
# production these are S3 and SES and the directories stay empty.
RUN mkdir -p /app/var/objectstore /app/var/outbox /app/var/inbox \
 && chown -R appuser:appuser /app/var

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=4).status==200 else 1)"

CMD ["uvicorn", "procureguard.api.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
