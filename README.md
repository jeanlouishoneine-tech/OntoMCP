# OntoMCP

**Ontology grounding for pharma and biotech scientists.**

OntoMCP is a client-agnostic MCP server and Jupyter extension for notebooks. It resolves
biological concepts to canonical ontology terms — cell death becomes `GO:0008219`, lung
adenocarcinoma becomes `MONDO:0005061` — with no hallucinated IDs and no API key required.
It works with any MCP-compatible client: Claude (Desktop / Code), GPT, Codex CLI, Cursor,
and others.

[![CI](https://github.com/jeanlouishoneine-tech/OntoMCP/actions/workflows/ci.yml/badge.svg)](https://github.com/jeanlouishoneine-tech/OntoMCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

---

## What it does

- **12 tools** — search, fetch, validate, map, annotate, and graph ontology terms
  (including direct `get_parents` / `get_children` alongside transitive `get_ancestors` / `get_descendants`)
- **11 ontologies** — GO, MONDO, HPO, ChEBI, UBERON, CL, EFO, MeSH, plus NCIT, DOID, PR
  (pharma/oncology) via the EBI OLS4 API
- **SQLite cache** — fast offline lookups, 7-day TTL, FTS5 full-text search
- **Client-agnostic MCP** — all 12 tools work in Claude (stdio) and GPT / Codex CLI / Cursor (SSE)
- **Jupyter extension** — search panel, interactive term graph, `%%ontomcp` cell magic

---

## Install

**macOS**
```bash
# Install uv via Homebrew (recommended on macOS)
brew install uv

git clone https://github.com/jeanlouishoneine-tech/OntoMCP.git
cd OntoMCP
make install
```

**Linux**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/jeanlouishoneine-tech/OntoMCP.git
cd OntoMCP
make install
```

**Windows**
```powershell
# Install uv via winget
winget install astral-sh.uv

git clone https://github.com/jeanlouishoneine-tech/OntoMCP.git
cd OntoMCP
uv sync --extra dev   # make is not available by default on Windows
```

> **Note:** `make` targets are not available on Windows without extra tooling (e.g. Git Bash or WSL).
> Use the equivalent `uv run ...` commands directly, or install [Make for Windows](https://gnuwin32.sourceforge.net/packages/make.htm).

For Jupyter support only:
```bash
uv sync --extra jupyter
```

---

## Quickstart

### Claude Desktop / Claude Code

1. Start the MCP server:
   ```bash
   make serve-mcp
   # or: uv run ontomcp-mcp
   ```

2. Add to your `claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "ontomcp": {
         "command": "uv",
         "args": ["run", "--directory", "/path/to/OntoMCP", "ontomcp-mcp"]
       }
     }
   }
   ```

3. Restart Claude Desktop. All 12 tools appear automatically.

**Try it:** Ask Claude — *"What is the ontology term for cell death?"* — and it will
return `GO:0008219` with definition, synonyms, and an ancestor graph.

---

### GPT / Codex CLI / other MCP clients (SSE)

Claude speaks MCP over stdio; GPT, Codex CLI, and remote clients speak it over
HTTP/SSE. Start OntoMCP in SSE mode — same entrypoint, same 12 tools, switched by
an environment variable:

```bash
ONTOMCP_TRANSPORT=sse make serve-mcp
# or: ONTOMCP_TRANSPORT=sse uv run ontomcp-mcp
# Server starts on http://127.0.0.1:8001
```

Point your OpenAI / Codex client at the SSE endpoint:

```
http://localhost:8001/sse
```

For remote access (non-localhost), set `ONTOMCP_MCP_HOST=0.0.0.0` and ensure the
port is reachable. The default bind is loopback (`127.0.0.1`) so the server is not
network-exposed unless you opt in.

Any MCP-compatible client that speaks the protocol over HTTP/SSE works with the
same endpoint.

---

### HTTP API

```bash
make serve-api
# or: uv run ontomcp-api
```

OpenAPI docs: [http://localhost:8000/docs](http://localhost:8000/docs)

```bash
# Search
curl -X POST localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "cell death", "ontologies": ["GO"]}'

# Fetch a term
curl localhost:8000/term/GO:0008219

# Health check
curl localhost:8000/health
```

---

### Jupyter Extension

```bash
make serve-api &   # API must be running
jupyter lab
```

```python
from ontomcp.jupyter_ext import OntoMCPClient, search_panel

search_panel(OntoMCPClient())
```

Type a concept, tick ontologies to restrict the search, and click **Search**.
Each result card has **Copy CURIE** and **Show graph** buttons. In the graph:
- teal = focus term
- gray = ancestor
- purple = descendant
- coral = sibling

Click any node for its term card. Double-click to re-centre the graph on it.

**Annotate a DataFrame column:**

```python
%load_ext ontomcp.jupyter_ext.magic
```
```python
%%ontomcp annotate --df cells --col cell_type --ontology CL
# adds cell_type_curie, cell_type_label, cell_type_score columns
```

---

## Configuration

| Environment variable   | Default              | Description                     |
|------------------------|----------------------|---------------------------------|
| `ONTOMCP_DB_PATH`      | `~/.ontomcp/cache.db`| SQLite cache file path          |
| `ONTOMCP_API_PORT`     | `8000`               | FastAPI server port             |
| `ONTOMCP_LOG_LEVEL`    | `INFO`               | Logging level                   |
| `ONTOMCP_API_URL`      | `http://localhost:8000` | Jupyter client base URL      |
| `ONTOMCP_TRANSPORT`    | `stdio`              | MCP transport: `stdio` (Claude) or `sse` (GPT/remote) |
| `ONTOMCP_MCP_HOST`     | `127.0.0.1`          | SSE bind address (use `0.0.0.0` to expose) |
| `ONTOMCP_MCP_PORT`     | `8001`               | SSE port (only used when `TRANSPORT=sse`) |

CLI flags override environment variables:
```bash
ontomcp-api --port 9000 --db-path /data/ontomcp.db
ontomcp-mcp --db-path /data/ontomcp.db
```

---

## Ontology Reference

| ID     | Name                              | Domain                   | Key use case                   |
|--------|-----------------------------------|--------------------------|--------------------------------|
| GO     | Gene Ontology                     | Gene function, processes | Omics, pathway analysis        |
| MONDO  | Mondo Disease Ontology            | Disease                  | Unified disease naming         |
| HPO    | Human Phenotype Ontology          | Clinical phenotypes      | Rare disease, genetics         |
| CHEBI  | Chemical Entities of Biological Interest | Small molecules   | Chemistry, pharmacology        |
| UBERON | Uberon Anatomy Ontology           | Cross-species anatomy    | Tissue, organ annotation       |
| CL     | Cell Ontology                     | Cell types               | Single-cell, immunology        |
| EFO    | Experimental Factor Ontology      | Experimental design      | GWAS, Open Targets             |
| MESH   | Medical Subject Headings          | Medical literature       | PubMed search, MeSH terms      |
| NCIT   | NCI Thesaurus                     | Cancer, drugs, indications | Pharma / oncology            |
| DOID   | Human Disease Ontology            | Disease                  | MONDO mapping target           |
| PR     | Protein Ontology                  | Proteins, complexes      | Drug targets                   |

All ontologies are free and served by the [EBI OLS4 API](https://www.ebi.ac.uk/ols4).
No API key required.

---

## Development

```bash
make test              # unit tests (no network)
make test-integration  # requires internet (hits EBI OLS4)
make lint              # ruff lint + format check
make format            # auto-format with ruff
make types             # mypy type check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution guide.

---

## License

MIT — see [LICENSE](LICENSE).
