# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-05-07

### Added

- **Docker Metrics Collection** — Async collector via `aiodocker` for CPU, RAM, and Health Status, compatible with cgroup v1/v2 across Linux/macOS/WSL.
- **Rules Engine** — YAML-driven rules with regex-based container matching, exclude patterns, multiple condition operators (`>`, `<`, `>=`, `<=`, `==`), and sustained-duration tracking.
- **Circuit Breaker** — SQLite-backed state manager that prevents Crash Loop BackOff by tracking restart counts per container within configurable time windows.
- **Strategy-based Actions** — Pluggable action system: `restart`, `stop`, `scale` (via `docker compose`), and `exec`.
- **Multi-channel Notifications** — Console, Discord (rich embeds), and Slack (Block Kit) via async webhooks.
- **Observability API** — Embedded FastAPI server exposing `/health`, `/history`, `/circuit-breakers`, and `/circuit-breakers/{name}/reset`.
- **Fail Fast Configuration** — Pydantic v2 validation of both `.env` settings and `rules.yaml` schema, rejecting invalid configs before daemon startup.
- **Structured Logging** — Loguru with JSON output for Datadog/ELK/Loki ingestion.
- **Docker Deployment** — Multi-stage Dockerfile with non-root user, Docker Compose with healthcheck, log rotation, and read-only socket mount.
- **Comprehensive Test Suite** — 197 unit and integration tests covering config validation (93), API endpoints (47), state manager/circuit breaker (37), and rules engine (20).
- **CI Pipeline** — GitHub Actions with ruff lint, mypy strict type checking, and pytest across Python 3.11, 3.12, and 3.13.
- **Code Quality** — Enforced formatting with `black` (line-length 100), linting with `ruff`, and strict type checking with `mypy`.
- **Documentation** — Comprehensive README with architecture diagram, API examples, and badges. Contributing guidelines with conventional commit convention, issue templates, and PR template.

### Security

- Docker socket mounted read-only in Docker Compose configuration.
- Non-root container user (`sentinel:sentinel`, UID 1000).
- No external network access required by the daemon process.
- Circuit Breaker prevents runaway automated restarts.

[1.0.0]: https://github.com/soneylegal/sentinel/releases/tag/v1.0.0
