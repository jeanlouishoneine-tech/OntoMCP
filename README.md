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

- **13 tools** — search, fetch, validate, map, annotate, and graph ontology terms
  (including direct `get_parents` / `get_children` alongside transitive `get_ancestors` / `get_descendants`)
- **Research profiles** — define your working set of ontologies once, load it by name forever.
  Seven starters ship ready to use; see [Research Profiles](#research-profiles)
- **10 default ontologies** — GO, MONDO, HPO, ChEBI, UBERON, CL, EFO, MeSH, plus NCIT and DOID
  (pharma/oncology) — with all ~286 OLS4 ontologies reachable by CURIE
- **SQLite cache** — fast offline lookups, 7-day TTL, FTS5 full-text search over labels,
  definitions **and synonyms**, with Porter stemming
- **Client-agnostic MCP** — all 13 tools work in Claude (stdio) and GPT / Codex CLI / Cursor (SSE)
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

3. Restart Claude Desktop. All 13 tools appear automatically.

**Try it:** Ask Claude — *"What is the ontology term for cell death?"* — and it will
return `GO:0008219` with definition, synonyms, and an ancestor graph.

---

### GPT / Codex CLI / other MCP clients (SSE)

Claude speaks MCP over stdio; GPT, Codex CLI, and remote clients speak it over
HTTP/SSE. Start OntoMCP in SSE mode — same entrypoint, same 13 tools, switched by
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
# Search — the envelope carries a `meta` block with the active profile,
# the exact scope searched, and whether the answer came from cache or OLS
curl -X POST localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "cell death", "ontologies": ["GO"]}'

# Fetch a term
curl localhost:8000/term/GO:0008219

# The rules and valid-ontology list for authoring a profile
curl localhost:8000/profile-setup-guidance

# Health check — reports the resolved scope for this session
curl localhost:8000/health
```

Requesting an ontology outside your profile returns `403` with a structured error that
distinguishes a real ontology you could add (`out_of_profile`, with its title) from a typo
(`unknown_ontology`, with `did_you_mean`).

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

## Research Profiles

OLS4 serves ~286 ontologies. Nobody works with all of them. A **research profile** is your
working set — 5–8 ontologies you name once and load by name forever, so you never re-describe
your research at the start of a session.

A profile bounds **discovery**, never **resolution**: `get_term`, the hierarchy tools and
`validate_term` work for any CURIE from any of the ~286, in or out of your profile. It only
decides what an unscoped `search_terms` looks through.

### Fastest path — use a starter

Seven starters ship ready to use. No setup, no file to write:

```json
{
  "mcpServers": {
    "ontomcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/OntoMCP", "ontomcp-mcp"],
      "env": { "ONTOMCP_PROFILE": "plant-breeding" }
    }
  }
}
```

| Starter | Ontologies |
|---|---|
| `pharma` | CHEBI, NCIT, MONDO, HPO, GO |
| `biotech-molecular` | GO, SO, CL, UBERON, CLO, CHEBI |
| `plant-breeding` | PO, TO, PECO, GO, ENVO, NCBITaxon\*, UO |
| `fermentation` | NCBITaxon, CHEBI, GO, ENVO, OMP, FOODON, UO, CHMO |
| `clinical-rare-disease` | MONDO, HPO, ORDO, SO, GENO, NCIT, UO |
| `microbiome` | NCBITaxon, CHEBI, GO, ENVO, OMP, UBERON, MONDO |
| `omics` | GO, CL, UBERON, EFO, MONDO, HPO |

\* held back from default search — see *Noisy ontologies* below.

### Register your own — just ask Claude

The conversational path needs no terminal. Ask:

> *"Set up my OntoMCP research profile — I work on CAR-T cell manufacturing."*

Claude calls `get_profile_setup_guidance` (the rules plus every valid prefix with its title),
proposes 5–8 ontologies **showing each one's full name**, waits for your explicit yes, then calls
`save_profile`. It takes effect immediately — no restart. Claude Desktop users can also pick the
**setup-research-profile** prompt directly.

Then load it the same way as a starter: `"ONTOMCP_PROFILE": "car-t-manufacturing"`.

### Update or customise it

Just ask — *"add FOODON to my profile"* — and Claude re-saves it. Or use the terminal:

```bash
ontomcp profile list                  # yours, plus the shipped starters
ontomcp profile show plant-breeding   # members, versions, what's held back and why
ontomcp profile create my-work --ontologies PO,TO,GO,ENVO,UO
ontomcp profile edit   my-work --ontologies PO,TO,GO,ENVO,UO,PECO
ontomcp profile check  my-work        # re-check member versions against OLS4
ontomcp profile refresh my-work       # re-freeze against a newer index (asks first)
ontomcp profile delete my-work
```

Profiles are plain JSON in `~/.ontomcp/profiles/`. Saving one under a starter's name shadows that
starter. You author two lines — `ontologies` and, rarely, `force_default`; everything under
`_derived` is written for you.

**Limits:** hard cap of 12 ontologies, 5–8 recommended. Every extra member adds noise to every
query, and anything you leave out is still resolvable by CURIE.

### Noisy ontologies

Two ontologies are held out of unscoped search by default because their labels collide with
ordinary words — **NCBITaxon** (2.8M taxa; genus and species tokens match everyday text) and
**PATO** (*increased*, *abnormal*, *small*). They stay in your profile and are searched the moment
you name them:

```
search_terms("Oryza sativa", ontologies=["NCBITAXON"])
```

If your domain lives in one of them, opt back in with `force_default` — as `fermentation` and
`microbiome` both do for NCBITaxon:

```bash
ontomcp profile edit my-work --ontologies NCBITAXON,CHEBI,GO --force-default NCBITAXON
```

When a search finds nothing in scope, OntoMCP says where the matches actually are — including
held-back members you already own — with the exact call to reach them. Set
`ONTOMCP_ESCAPE_HATCH=0` to turn that off.

### Locking scope for a team

`ONTOMCP_ONTOLOGIES` overrides everything, resolves verbatim (no held-back split), and disables
profile writing — for administrators distributing a fixed configuration:

```json
"env": { "ONTOMCP_ONTOLOGIES": "PO,TO,GO" }
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
| `ONTOMCP_PROFILE`      | *(unset)*            | Research profile to load by name — a starter or one of your own. Unset = the 10 default ontologies |
| `ONTOMCP_ONTOLOGIES`   | *(unset)*            | Administrator hard-lock, e.g. `PO,TO,GO`. Overrides `ONTOMCP_PROFILE` and disables profile writing |
| `ONTOMCP_PROFILE_DIR`  | `~/.ontomcp/profiles`| Where your profiles are stored |
| `ONTOMCP_ESCAPE_HATCH` | `1`                  | Set `0` to suppress "no matches in scope, but they exist here" guidance |

Three entrypoints are installed:

```bash
ontomcp profile show pharma            # profiles (see Research Profiles)
ontomcp-mcp --db-path /data/ontomcp.db # MCP server; CLI flags override env vars
ontomcp-api --port 9000 --db-path /data/ontomcp.db
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

All ontologies are free and served by the [EBI OLS4 API](https://www.ebi.ac.uk/ols4).
No API key required.

These ten are the **default scope**, not the limit. Resolution — `get_term`, the hierarchy
tools, `validate_term` — works for any CURIE from any of the ~286 ontologies OLS4 serves,
whether or not it is in scope. Scope bounds *discovery*, never *resolution*.

> **Note:** PR (Protein Ontology) was removed in 2026-08. OLS4 no longer serves it —
> `/ontologies/pr` returns 404 — so every PR lookup was failing silently.

### What freezing a profile guarantees, and what it does not

Freezing a research profile pins **scope**: which ontologies are resolvable and searchable, at
which index version, with which quarantine split and aliases. That resolution is deterministic
and reproducible offline — the same profile yields the same scope on any machine, at any later
date, with no network call.

Freezing does **not** pin **content**. Term labels, definitions, synonyms and obsolescence
status are fetched live from OLS4 and cached per machine for 7 days, and result ordering is an
ordinal position over whichever backend served the query. Two machines can return different
labels for the same CURIE on the same day, and the same machine can return a different label
eight days later.

**The CURIE is the stable deliverable; the surrounding content is not archived.** If your
workflow must reproduce term *content* — a regulated submission, an audit trail, a GxP record —
record the CURIEs and the ontology release versions yourself at the time of annotation.
OntoMCP grounds; it does not archive.

Two related honesty notes: the resolvable scope is pinned, but *which subset a given query
searches* is the client model's choice — made auditable by `meta.scope` on every search
response. And the starter profiles remove the *description* step, not the *setup* step.

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
