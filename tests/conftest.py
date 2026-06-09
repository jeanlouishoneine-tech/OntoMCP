"""Shared pytest fixtures. Real cache/OLS fixtures land in Phase 2+.

The package is installed (src-layout via `uv sync`), so `import ontomcp` works
without path hacks here.
"""

from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Path to an isolated, non-existent SQLite file for cache tests."""
    return tmp_path / "test_cache.db"
