# ---- Stage 1: build the React frontend ------------------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# VITE_API_BASE_URL is intentionally unset: a production build calls the API on the same origin.
RUN npm run build

# ---- Stage 2: API image (also serves the built frontend) ------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System libraries. python:3.12-slim does not ship libexpat, which the GDAL bundled inside the Fiona
# wheel links against (without it: "ImportError: libexpat.so.1: cannot open shared object file").
# Everything else GDAL needs (PROJ, GEOS, curl, sqlite, ...) is bundled in the manylinux wheels.
# Fiona publishes no Linux arm64 wheels, so on aarch64 (Docker on Apple Silicon) it is compiled
# against the system GDAL instead, which needs the build toolchain and GDAL headers as well.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && if [ "$(uname -m)" = "aarch64" ]; then \
        apt-get install -y --no-install-recommends build-essential libgdal-dev gdal-bin; \
    fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY --from=frontend /frontend/dist ./frontend/dist

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app
USER appuser

ENV UPLOAD_DIR=/app/uploads \
    FRONTEND_DIST_DIR=/app/frontend/dist
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health')" || exit 1

# Single worker on purpose: processing runs as in-process background tasks (see README).
# $PORT is injected by Render; defaults to 8000 locally.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
