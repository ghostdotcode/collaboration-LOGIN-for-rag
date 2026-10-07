# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — builder: compile wheels with the toolchain, then throw it away.
# Keeps gcc/libpq-dev out of the runtime image (smaller surface, fewer CVEs).
#
# Pinned to 3.11 because the distroless runtime below ships Python 3.11, and a
# venv's site-packages path is version-specific — one builder must satisfy both.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — distroless: the hardened image, built on demand with
# `docker build --target distroless`.
#
# No shell, no package manager, no libc utilities: nothing for an attacker with
# RCE to pivot into. The trade-off is that `docker exec` and HEALTHCHECK are
# unavailable, so the Kubernetes probes are the only health signal — which is
# what production relies on anyway.
# ─────────────────────────────────────────────────────────────────────────────
FROM gcr.io/distroless/python3-debian12:nonroot AS distroless

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/venv/lib/python3.11/site-packages

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=nonroot:nonroot alembic.ini ./
COPY --chown=nonroot:nonroot alembic ./alembic
COPY --chown=nonroot:nonroot app ./app

USER nonroot
EXPOSE 8000

ENTRYPOINT ["python", "-m", "uvicorn", "app.main:app", \
            "--host", "0.0.0.0", "--port", "8000", \
            "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]

# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — runtime (default target): just Python, the venv and the app,
# running as non-root. Keeps a shell and curl, which makes incident debugging
# and `docker run` health checks possible in non-production environments.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# libpq5 only (the runtime library, not the -dev headers).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 lms \
    && useradd --uid 10001 --gid lms --no-create-home --shell /usr/sbin/nologin lms

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=lms:lms alembic.ini ./
COPY --chown=lms:lms alembic ./alembic
COPY --chown=lms:lms app ./app

USER lms
EXPOSE 8000

# Matches the Kubernetes liveness probe, so `docker run` behaves like a pod.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

# Two workers per pod; horizontal scaling is the HPA's job, not uvicorn's.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
