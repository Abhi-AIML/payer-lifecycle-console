# Payer Lifecycle Console on Cloud Run.
# Built by Cloud Build (cloudbuild.yaml). Only backend/, data/ and portals/ are copied in (see .dockerignore);
# secrets are never baked in: Cloud Run mounts them from Secret Manager as environment variables.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ backend/
COPY data/ data/
COPY portals/ portals/

# Run as an unprivileged user that cannot write to the application files.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app
WORKDIR /app/backend

# One worker: all demo state and sessions live in this process's memory.
# --forwarded-allow-ips="*": the only route in is Google's front end, which terminates TLS and sets X-Forwarded-Proto,
# so the app sees https and marks its session cookie Secure.
# --no-access-log: Cloud Run already writes a request log (status, latency) for every request.
CMD exec uvicorn app:app --host 0.0.0.0 --port "${PORT}" --workers 1 \
    --proxy-headers --forwarded-allow-ips="*" --no-server-header --no-access-log
