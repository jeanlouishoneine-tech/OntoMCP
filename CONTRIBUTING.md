# Contributing to OntoMCP

Thank you for taking the time to contribute. OntoMCP is an open-source project and
welcomes contributions of all kinds — bug reports, documentation improvements, new
features, and code reviews.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How to Report a Bug](#how-to-report-a-bug)
- [How to Request a Feature](#how-to-request-a-feature)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Pull Request Checklist](#pull-request-checklist)
- [Coding Standards](#coding-standards)
- [Testing](#testing)

---

## Code of Conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/)
Code of Conduct. Be respectful, constructive, and welcoming to all contributors.

---

## How to Report a Bug

1. Search [existing issues](https://github.com/jeanlouishoneine-tech/OntoMCP/issues)
   to avoid duplicates.
2. Open a new issue using the **Bug Report** template.
3. Include: OntoMCP version, Python version, OS, a minimal reproducible example, and
   the full error traceback.

---

## How to Request a Feature

1. Search existing issues and discussions first.
2. Open a new issue using the **Feature Request** template.
3. Describe the use case clearly — who benefits and why it matters.

---

## Development Setup

OntoMCP uses [`uv`](https://github.com/astral-sh/uv) for environment and dependency
management.

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone git@github.com:<your-username>/OntoMCP.git
cd OntoMCP

# 2. Install uv (if not already installed)
# macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell):
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 3. Create the virtual environment and install all dependencies
make install

# 4. Verify the setup
make test
```

---

## Making Changes

1. **Create a branch** from `main` using the naming convention:
   - `feature/short-description` for new features
   - `fix/short-description` for bug fixes
   - `docs/short-description` for documentation changes

   ```bash
   git checkout -b feature/add-obo-ontology
   ```

2. **Make small, focused commits.** One logical change per commit.
   Write commit messages in imperative form:
   ```
   Add OBO ontology support to suggest_ontology
   Fix CURIE prefix normalisation for MeSH terms
   ```

3. **Run checks locally before pushing:**
   ```bash
   make lint    # ruff lint + format check
   make types   # mypy type check
   make test    # unit tests (no network required)
   ```

4. **Push your branch** and open a Pull Request against `main`.

---

## Pull Request Checklist

Every PR must satisfy the following before it will be reviewed:

- [ ] All unit tests pass: `make test`
- [ ] No lint errors: `make lint`
- [ ] No new type errors: `make types`
- [ ] New functionality is covered by tests
- [ ] The PR description explains *what* changed and *why*
- [ ] If touching the OLS client, integration tests are updated or added
- [ ] No secrets, credentials, or local file paths committed
- [ ] CHANGELOG.md is updated under `[Unreleased]`

---

## Coding Standards

- **Python 3.11+** — use modern syntax (`X | Y` unions, `match`, etc.)
- **Formatting and linting:** `ruff` — run `make lint` to check
- **Type hints:** required on all public functions
- **No business logic in `mcp_server/` or `api/`** — all logic lives in `core/`
- **Cache-first:** always check SQLite before hitting OLS
- **No hardcoded secrets or paths** — use `config.py` and env vars
- **Error handling:** tools must never raise unhandled exceptions — return structured
  error dicts instead
- **Comments:** only where the *why* is non-obvious; do not paraphrase the code

See [CLAUDE.md](CLAUDE.md) for the full architecture constraints.

---

## Testing

```bash
# Unit tests only (no network — always required to pass)
make test

# Include integration tests (requires internet access to EBI OLS4)
make test-integration
```

- Unit tests mock the OLS client — never hit the network in unit tests.
- Integration tests are marked `@pytest.mark.integration`.
- `pytest -m "not integration"` must pass clean before any commit.

---

## Questions?

Open a [Discussion](https://github.com/jeanlouishoneine-tech/OntoMCP/discussions) or
file an issue. We are happy to help.
