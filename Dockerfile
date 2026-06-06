# ─────────────────────────────────────────────────────────
# Sentinel - Production Dockerfile
# Multi-stage build for optimized dependency caching
# ─────────────────────────────────────────────────────────

# --- Stage 1: Dependencies Builder ---
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy configuration files
COPY pyproject.toml README.md ./

# Create dummy package structure to satisfy setup tools during dependency install
RUN mkdir src && touch src/__init__.py

# Install production dependencies
RUN pip install --no-cache-dir .

# --- Stage 2: Final Runtime ---
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy installed dependencies from builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source code and rules config
COPY src/ ./src/
COPY rules.yaml .

# Create db directory
RUN mkdir -p /app/db

# Non-root user for security
RUN groupadd --gid 1000 sentinel && \
    useradd --uid 1000 --gid sentinel --shell /bin/bash sentinel && \
    chown -R sentinel:sentinel /app

USER sentinel

EXPOSE 9120

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9120/health')" || exit 1

ENTRYPOINT ["python", "-m", "src.main"]