# Contributing to hermes-telemetry

Thanks for your interest in contributing! This document explains how to set up the project, run tests, and submit changes.

## Table of Contents

- [Project Overview](#project-overview)
- [Development Setup](#development-setup)
- [Code Style](#code-style)
- [Testing](#testing)
- [Architecture](#architecture)
- [Making Changes](#making-changes)
- [Commit Conventions](#commit-conventions)
- [Pull Requests](#pull-requests)
- [Releasing](#releasing)

---

## Project Overview

`hermes-telemetry` is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that captures real usage data (tokens, cost, latency, tool calls) and enforces budget guardrails. It consists of:

> **New contributor?** Read [ONBOARDING.md](ONBOARDING.md) first — it documents every non-obvious design decision from v0.1 through v0.4.0, including Hermes hook constraints, the concurrency model, and why the budget enforcement works the way it does.

- **Hook pipeline** — captures telemetry via Hermes lifecycle hooks (`on_session_start`, `post_api_request`, `post_tool_call`, etc.)
- **SQLite database** — stores all telemetry data locally
- **Pricing engine** — local pricing table × tokens = estimated cost
- **Budget enforcement** — configurable daily/monthly limits with soft/hard enforcement
- **Dashboard** — standalone HTML/JS dashboard served locally (port 8765)
- **Slash commands** — `/stats`, `/budget`, `/setup` for in-agent interaction

## Development Setup

### Prerequisites

- Python 3.8+
- Hermes Agent installed and configured
- Git

### Clone and install

```bash
git clone https://github.com/nujovich/hermes-telemetry.git
cd hermes-telemetry

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

### Verify the setup

Run the exact same checks CI runs, in the same order (the CI `test` job
`needs: lint`, and lint runs `ruff format --check` **first** — so a formatting
miss fails the whole pipeline, tests included):

```bash
# One-liner: format check → lint → tests (matches .github/workflows/ci.yml)
ruff format --check . && ruff check . && pytest tests/ -v
```

### Enable the pre-commit hook (recommended)

A versioned hook in [`.githooks/pre-commit`](.githooks/pre-commit) runs the same
three checks before every commit, so you never push a red CI. Enable it once per
clone:

```bash
git config core.hooksPath .githooks
```

Bypass for a single commit with `git commit --no-verify` (use sparingly).

### Enable the plugin for local development

```bash
# Option A: symlink into Hermes plugins directory
ln -s $(pwd) ~/.hermes/plugins/hermes-telemetry
hermes plugins enable hermes-telemetry
hermes gateway restart

# Option B: install from local path
hermes plugins install $(pwd)
hermes plugins enable hermes-telemetry
hermes gateway restart
```

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for both linting and formatting.

| Rule set | Purpose |
|----------|---------|
| `E`, `W` | pycodestyle errors/warnings |
| `F` | pyflakes |
| `I` | isort |
| `UP` | pyupgrade |
| `B` | flake8-bugbear |
| `SIM` | flake8-simplify |

Key conventions:

- **Line length:** 100 characters
- **Quotes:** double (`"`)
- **Indentation:** 4 spaces
- **Type hints:** encouraged but not required (mypy is optional)
- **Docstrings:** Google style for public functions

Auto-fix lint issues:

```bash
ruff check . --fix
ruff format .
```

## Testing

### Running tests

```bash
# All tests
pytest tests/ -v

# Specific test file
pytest tests/test_budget.py -v

# Specific test
pytest tests/test_budget.py::test_budget_soft_limit -v

# With coverage
pytest tests/ -v --cov=. --cov-report=term-missing
```

### Test structure

| File | What it tests |
|------|---------------|
| `test_init.py` | Plugin initialization, hook registration, config generation |
| `test_db.py` | Database schema, migrations, CRUD operations |
| `test_pricing.py` | Pricing table loading, cost calculation, auto-refresh |
| `test_pricing_hot_reload.py` | Pricing cache hot-reload after `/setup pricing auto` |
| `test_budget.py` | Budget enforcement (soft/hard limits, degradation) |
| `test_stats_providers.py` | `/stats providers` output, provider breakdown |
| `test_stats_models.py` | `/stats models` output, per-model breakdown |
| `test_subagent_reconciliation.py` | Subagent session tracking and cost attribution |
| `test_setup.py` | `/setup` command, PoC flow |
| `test_isolation.py` | Isolation contract — guards that no test reads the real `~/.hermes` |
| `test_dashboard.py` | Dashboard CLI arg parsing (`dashboard/serve.py`) |

### Test isolation

**Tests never read or write your real `~/.hermes`.** A project-level autouse
fixture in [`conftest.py`](conftest.py) (`isolate_hermes_home`) redirects
`HERMES_HOME` to a fresh per-test temp directory, so every test gets a clean,
empty Hermes home. `HOME`/`USERPROFILE` are pinned to the same temp dir as a
safety net, so even a stray `Path.home()` fallback can only ever reach the temp
dir — never your real files.

- **Pricing tests use a committed fixture** ([`tests/fixtures/pricing.yaml`](tests/fixtures/pricing.yaml))
  instead of your machine's auto-refreshed `~/.hermes/telemetry/pricing.yaml`, so
  results are deterministic and don't depend on local state.
- **The isolation guarantee is enforced**, not just convention:
  [`tests/test_isolation.py`](tests/test_isolation.py) is fail-closed — it plants
  a poisoned file in a decoy home and asserts no code path reads it. New code
  **must** locate Hermes files via `HERMES_HOME` (e.g. the `_budget_path()` /
  `_pricing_path()` helpers), never `Path.home()` directly.
- **Practical implication:** you can run the suite safely with a live gateway and
  real data — it won't touch them. To verify isolation, point `HERMES_HOME` at a
  temp dir; **never `mv`/`rm` your real `~/.hermes`** to "prove" it (a running
  gateway will fight you, and you risk your real data).

#### Where do tests actually read/write?

Each test gets a unique, throwaway directory that **pytest** creates on the OS
temp filesystem — it is *not* your `~/.hermes`, *not* a path in the repo or a
venv, and *not* a permanent environment variable:

```
/tmp/pytest-of-<user>/pytest-<N>/<test_name><i>/telemetry/{telemetry.db,pricing.yaml,budget.yaml}
```

This path comes from pytest's built-in [`tmp_path`](https://docs.pytest.org/en/stable/how-to/tmp_path.html)
fixture — we don't configure it. pytest mints a fresh dir per test function,
keeps only the last few runs (`pytest-11/12/13/…`), and cleans up older ones.
The `isolate_hermes_home` fixture just reads that path and points
`HERMES_HOME`/`HOME` at it; `test_pricing.py` uses its own
`tmp_path_factory.mktemp("pricing_home")` dir seeded with the committed fixture.
Override the base dir with `pytest --basetemp=DIR` (default: `/tmp/pytest-of-<user>/`).

### Writing tests

- Use `pytest` (no `unittest` boilerplate needed)
- Test file names: `test_*.py`
- Test function names: `test_*`
- Use fixtures from `conftest.py` for shared setup — the autouse
  `isolate_hermes_home` already gives you a clean `HERMES_HOME`; module fixtures
  add domain-specific setup (DB connection reset, cache clears) on top
- Mock external HTTP calls (OpenRouter API, pricing fetch) — never hit real APIs in tests
- Keep tests fast and isolated — each test gets a fresh temp DB under its own
  `HERMES_HOME`; if you need custom pricing/budget data, write it into that temp
  dir (or copy a committed fixture), never the real home

## Architecture

```
hermes-telemetry/
├── __init__.py          # Plugin entry point, hook registration, slash commands
├── db.py                # SQLite schema, migrations, queries
├── pricing.py           # Pricing table management, cost calculation
├── pricing_refresh.py   # Auto-refresh pricing from OpenRouter API
├── budget.py            # Budget enforcement logic
├── stats.py             # /stats command implementation
├── setup.py             # /setup command, PoC flow
├── setup_cmd.py         # Setup command helpers
├── plugin.yaml          # Plugin metadata (name, version, hooks)
├── config.example.yaml  # Example configuration
├── pricing.example.yaml # Example pricing table
├── budget.example.yaml  # Example budget limits
├── dashboard/           # Standalone HTML/JS dashboard
│   └── serve.py         # stdlib HTTP server + Chart.js
├── docs/                # Documentation and screenshots
└── tests/               # Test suite
```

### Hook pipeline

The plugin hooks into Hermes Agent lifecycle events:

```
on_session_start     → create session record, check budget
pre_api_request      → estimate tokens, check budget (pre-call)
post_api_request     → record real usage + cost, update budget
post_tool_call       → record tool latency + success/failure
on_session_end       → finalize session, compute wall time
subagent_stop        → attribute subagent cost to parent session
```

### Database

SQLite with WAL mode. Schema is managed via incremental migrations in `db.py`. The database lives at `~/.hermes/hermes-telemetry/telemetry.db` by default.

### Configuration

Three YAML files control behavior:

| File | Purpose | Auto-generated? |
|------|---------|-----------------|
| `pricing.yaml` | Model pricing (input/output per-token cost) | Yes, on first run |
| `budget.yaml` | Daily/monthly limits, enforcement level | Yes, on first run |
| `config.yaml` | Plugin settings (DB path, dashboard port, etc.) | No, manual |

## Making Changes

### Workflow

1. Create a branch: `git checkout -b feat/my-feature` (or `fix/my-bug`)
2. Make your changes
3. Run tests: `pytest tests/ -v`
4. Run lint: `ruff check . && ruff format --check .`
5. Update `CHANGELOG.md` for user-facing changes
6. Commit (see conventions below)
7. Push and open a PR

### What to change where

| Change type | Files to modify |
|-------------|-----------------|
| New hook | `__init__.py` + `db.py` (schema) + `stats.py` (display) + tests |
| New pricing field | `pricing.py` + `pricing.example.yaml` + tests |
| New budget rule | `budget.py` + `budget.example.yaml` + tests |
| New slash command | `__init__.py` + new `*_cmd.py` + tests |
| Dashboard changes | `dashboard/serve.py` + `dashboard/index.html` |
| Config option | `config.example.yaml` + `__init__.py` (loading) + tests |

## Commit Conventions

We use [Conventional Commits](https://www.conventionalcommits.org/):

| Prefix | Use for |
|--------|---------|
| `feat:` | New feature or capability |
| `fix:` | Bug fix |
| `refactor:` | Code change with no behavioral change |
| `docs:` | Documentation only |
| `chore:` | CI, deps, tooling, packaging |
| `test:` | Adding or correcting tests |

Examples:

```
feat: add per-tool cost breakdown to /stats
fix: correct token estimation when usage=None
refactor: extract pricing table validation to helper
docs: add architecture diagram to README
chore: bump ruff to 0.5.0
test: add budget degradation edge case
```

## Pull Requests

1. Fill out the PR template (in `.github/PULL_REQUEST_TEMPLATE.md`)
2. Ensure all tests pass and lint is clean
3. Update `CHANGELOG.md` for user-facing changes
4. Bump version in `__init__.py`, `pyproject.toml`, and `plugin.yaml` if releasing
5. Link any related issues

### PR review checklist

- [ ] Tests pass locally (`pytest tests/ -v`)
- [ ] Lint passes (`ruff check . && ruff format --check .`)
- [ ] `CHANGELOG.md` updated (user-facing changes)
- [ ] Version bumped (if releasing)
- [ ] No secrets or credentials committed
- [ ] Documentation updated if behavior changed

## Releasing

1. Bump version in all three files:
   - `__init__.py` → `__version__ = "X.Y.Z"`
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `plugin.yaml` → `version: X.Y.Z`
2. Update `CHANGELOG.md` with release date and summary
3. Commit: `chore: bump version X.Y.Z`
4. Tag: `git tag vX.Y.Z`
5. Push: `git push origin main --tags`
6. Create GitHub release from tag

### Versioning

We use [Semantic Versioning](https://semver.org/):

- **MAJOR** (X.0.0): Breaking changes to config format, DB schema, or command output
- **MINOR** (0.X.0): New features, new commands, new metrics
- **PATCH** (0.0.1): Bug fixes, minor improvements

---

## Questions?

Open an issue at [github.com/nujovich/hermes-telemetry/issues](https://github.com/nujovich/hermes-telemetry/issues) or reach out on [dev.to @nujovich](https://dev.to/nujovich).
