# Contributing to Sentinel

Thank you for considering contributing to **Sentinel**! This document provides guidelines and best practices for contributing to this project.

## 📋 Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Development Setup](#development-setup)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Commit Convention](#commit-convention)
- [Opening a Pull Request](#opening-a-pull-request)
- [Review Process](#review-process)

---

## Code of Conduct

This project is licensed under the [Apache License 2.0](LICENSE). By participating, you agree to maintain a respectful and inclusive environment. Be kind, be constructive, and focus on the work.

---

## Development Setup

### Prerequisites

- Python 3.11+
- Docker Engine (for integration tests with real containers — optional)
- Git

### Setup

```bash
# 1. Fork and clone the repository
git clone https://github.com/<your-username>/sentinel.git
cd sentinel

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install all dependencies (core + dev)
pip install -r requirements.txt

# 4. Copy environment configuration
cp .env.example .env

# 5. Verify everything works
python -m pytest tests/ -v
```

> **Note:** The test suite does NOT require a running Docker daemon. All Docker interactions are fully mocked via `tests/conftest.py`.

---

## Running Tests

```bash
# Run the full test suite (197 tests)
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_config.py -v

# Run a specific test class
python -m pytest tests/test_state_manager.py::TestCrashLoopSimulation -v

# Run with short traceback (CI default)
python -m pytest tests/ --tb=short
```

---

## Code Style

We enforce consistent code quality with three tools. **All three must pass before a PR can be merged.**

### Formatter: black

```bash
# Check formatting (dry run)
black --check src/ tests/

# Apply formatting
black src/ tests/
```

Configuration: `pyproject.toml` → `[tool.black]` (line-length: 100, target: py311).

### Linter: ruff

```bash
# Check for lint errors
ruff check src/ tests/

# Auto-fix where possible
ruff check --fix src/ tests/
```

Configuration: `pyproject.toml` → `[tool.ruff]` / `[tool.ruff.lint]`.

### Type Checker: mypy

```bash
# Strict type checking (production code only)
mypy src/ --strict
```

Configuration: `pyproject.toml` → `[tool.mypy]`. Third-party libraries without stubs (aiodocker, aiosqlite, loguru) are configured in `[[tool.mypy.overrides]]`.

---

## Commit Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/). Every commit message must follow this format:

```
<type>(<scope>): <short description>

<optional body>
```

### Types

| Type | When to use |
|---|---|
| `feat` | New feature or functionality |
| `fix` | Bug fix |
| `test` | Adding or updating tests |
| `docs` | Documentation changes |
| `ci` | CI/CD pipeline changes |
| `chore` | Maintenance (deps, formatting, configs) |
| `refactor` | Code changes that don't fix bugs or add features |
| `perf` | Performance improvements |

### Scopes (optional)

| Scope | Module |
|---|---|
| `core` | `src/core/` (config, logger, exceptions) |
| `engine` | `src/engine/` (rules, state_manager) |
| `api` | `src/api/` (routes, server) |
| `actions` | `src/actions/` (restart, stop, scale) |
| `notifiers` | `src/notifiers/` (console, discord, slack) |
| `collectors` | `src/collectors/` (docker_async) |

### Examples

```
feat(engine): add sustained-duration tracking for conditions
fix(collectors): handle cgroup v2 memory accounting
test(core): add unit tests for config validation
docs: update README with architecture diagram
ci: add python 3.13 to test matrix
chore: upgrade pydantic to 2.8.0
```

---

## Opening a Pull Request

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```

2. **Make your changes** following the code style guidelines above.

3. **Ensure all checks pass locally:**
   ```bash
   black --check src/ tests/
   ruff check src/ tests/
   mypy src/ --strict
   python -m pytest tests/ -v
   ```

4. **Commit** using the conventional commit format.

5. **Push** and open a PR against `main`.

6. **Fill out the PR template** — it will appear automatically.

---

## Review Process

1. **CI must pass** — all GitHub Actions checks (lint, type check, tests across 3.11/3.12/3.13) must be green.
2. **Code review** — at least one maintainer will review the changes.
3. **No force-pushes** after review has started.
4. **Squash merge** is preferred for clean history.

### What reviewers look for

- [ ] Tests cover the new/changed behavior
- [ ] No regressions in existing tests
- [ ] Code follows established patterns (Strategy for actions/notifiers, Pydantic for config)
- [ ] Docstrings on public functions/classes
- [ ] No hardcoded values — use configuration
- [ ] Circuit Breaker is respected for destructive actions

---

## Questions?

Open an [issue](https://github.com/soneylegal/sentinel/issues) or start a [discussion](https://github.com/soneylegal/sentinel/discussions). We're happy to help!
