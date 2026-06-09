# OntoMCP — Project Rules

## MCP Tool Design

- Tool docstrings are instructions to the LLM. Write them to describe *when* to call the tool, not what the code does.
- Every tool must have explicit `Args:` documentation — Claude uses these to construct calls.
- Tool names and parameter names must be self-explanatory without reading the docstring.
- Tools must never raise unhandled exceptions — return structured error dicts instead.
- Keep tool output payloads small. Enforce the hard caps defined in `plan.md` (40/50 node limits, 500-term max).

## Architecture Constraints

- No business logic in `mcp_server/` or `api/` — all logic lives in `core/`.
- Cache-first on every OLS call. Never skip SQLite check.
- Never cache `validate_term` results — always hit OLS live.
- Both servers read and write the cache through the shared `core/` (cache-first with write-back on miss); neither server touches SQLite directly. Concurrency safety comes from SQLite WAL mode plus `busy_timeout` (see `config.BUSY_TIMEOUT_MS`), not from restricting writes to one process. All writes must be idempotent (upsert / insert-or-ignore) so concurrent writers converge.
- Every connection must set WAL mode, foreign keys, and the busy timeout on open (`cache._connect`).

## OLS API Etiquette

- Check cache before any outbound request.
- 7-day TTL for term data.
- Retry only on 429 and 5xx — max 3 attempts, exponential backoff.
- Always send `User-Agent: OntoMCP/0.1`.

## CURIE Rules

- Store and return CURIEs with uppercase prefix: `GO:0008219`, never `go:0008219`.
- Strip `obo:` prefix if OLS returns it.
- Use `IRI_TEMPLATES` in `config.py` for all IRI construction — no ad-hoc string building.

## Testing

- Unit tests mock the OLS client — never hit the network in unit tests.
- Integration tests are marked `@pytest.mark.integration` and can be skipped offline.
- `pytest -m "not integration"` must always pass clean before any commit.
