.PHONY: install test test-integration lint format types serve-api serve-mcp clean

install:
	uv sync --extra dev --extra jupyter

test:
	uv run pytest -m "not integration" --tb=short -q

test-integration:
	uv run pytest -m integration --tb=short -q

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

types:
	uv run mypy src/ontomcp/

serve-api:
	uv run ontomcp-api

serve-mcp:
	uv run ontomcp-mcp

clean:
	rm -rf .venv dist build __pycache__ .pytest_cache .mypy_cache .ruff_cache
