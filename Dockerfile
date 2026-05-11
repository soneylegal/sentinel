# ─────────────────────────────────────────────────────────
# Sentinel - Production Dockerfile
# Multi-stage build for minimal image size
# ─────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Em vez de requirements.txt, copiamos a configuração moderna do projeto
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY rules.yaml .

# Instala o projeto e as dependências listadas no pyproject.toml
RUN pip install --no-cache-dir .

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