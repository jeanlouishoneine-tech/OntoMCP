# Changelog

All notable changes to OntoMCP are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
OntoMCP uses [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
- Research profiles (PR #1): a 5–8 ontology working set that scopes `search_terms`,
  `bulk_annotate` and `map_across_ontologies`, built from a committed OLS4 index of ~286
  ontologies; seven shipped starters; `get_profile_setup_guidance` and `save_profile` tools and
  the `setup-research-profile` prompt; an escape hatch that reports matches outside the scope;
  canonical IRI resolution with disconfirmation; the `ontomcp profile` CLI; the
  `ONTOMCP_PROFILE`, `ONTOMCP_ONTOLOGIES` (admin lock) and `ONTOMCP_PROFILE_DIR` settings
- `get_profile` and `update_profile` tools (15 tools in total): see the profile in use, add or
  remove ontologies, reset to the defaults, or undo the last change from the conversation
- In-client confirmation for every profile write: an elicitation listing each ontology with its
  full title (and Adding/Removing for changes) on clients that support it; on clients that do
  not, the client's own tool-permission prompt. Decline, cancel, timeout or error writes nothing
- Strict mode (`ONTOMCP_REQUIRE_CONFIRMATION=1`) refuses profile writes on clients that cannot
  confirm in-client; `ONTOMCP_ELICIT_TIMEOUT_SECONDS` (default 300)
- One level of undo for profile saves and deletes (`<name>.json.prev`)
- Tool annotations on every MCP tool: reads are read-only, the two write tools destructive
- `ontomcp profile use`, `undo` and `reset` CLI commands
- `get_parents` / `get_children` tools and `/term/{curie}/parents` `…/children` routes:
  true one-hop `is_a` edges, the new source of truth for the relationships table and
  `get_term_graph`
- Obsolete-term `consider` alternates surfaced in `get_term` and `validate_term`
- `warnings` on `get_term` for obsolete terms and `do_not_annotate`-subset terms
- `is_obsolete` flag on every `search_terms` result
- `mapping_predicate` on `map_across_ontologies` results (`skos:exactMatch` vs
  `heuristic_label`), and curated `annotation.database_cross_reference` xrefs
- Provenance on `get_term`: `definition_sources`, `subsets`, `has_children`/`is_leaf`,
  and `ontology_version` (also reported in `/health`)
- Three pharma/oncology ontologies: NCIT, DOID, PR
- Dual MCP transport: stdio (default, Claude) and SSE (`ONTOMCP_TRANSPORT=sse`)
  for GPT, Codex CLI, and remote clients — the server is now client-agnostic
- `.env.example` documenting all environment variables
- README "Connecting clients" section covering both transports
- MIT License
- CONTRIBUTING.md with development setup and PR checklist
- GitHub CI workflow: pytest, ruff, mypy on every pull request
- GitHub PR template and issue templates (bug report, feature request)
- Makefile with `install`, `test`, `lint`, `types`, `serve-api`, `serve-mcp` targets
- `pyproject.toml`: author metadata, project URLs, ruff and mypy configuration

### Changed
- `get_ancestors` / `get_descendants` now correctly report the **transitive** closure
  (`depth="transitive"`) and no longer record transitive pairs as direct edges; use
  `get_parents` / `get_children` for one-hop relations. Hierarchy nodes carry honest
  `rel_type` (`is_a` for direct, `ancestor`/`descendant` for transitive) instead of a
  blanket `is_a`
- `map_across_ontologies` heuristic label matches are capped below curated xrefs and are
  reported as candidates, not asserted equivalences
- Cache schema gained `consider`, `subsets`, `definition_sources`, `has_children`,
  `is_leaf` columns and an `ontology_versions` table; existing cache files upgrade in
  place (additive migration)

### Fixed
- A saved profile is now the profile in use. Before, `save_profile` replied "active now" but
  searches kept using the built-in set unless the profile was named `default` or pinned with
  `ONTOMCP_PROFILE`. An active-profile pointer (`~/.ontomcp/profiles/.active`) now sits between
  `ONTOMCP_PROFILE` and `default.json`, and every write reports the profile actually in use
- Graph topology corruption: edges no longer fabricate a direct link between a term and a
  distant transitive ancestor
- `make types` / CI mypy ran against a non-existent `ontomcp/` path (exited 0
  without checking); now points at `src/ontomcp/` and the codebase is mypy-clean
- Resolved 9 latent type errors surfaced by the corrected mypy path

---

## [0.1.0] - Unreleased

Initial release.

### Added
- SQLite cache layer with FTS5 full-text search and WAL mode (`core/cache.py`)
- Async OLS4 API client with retry logic (`core/ols_client.py`)
- 10 core tool functions: `search_terms`, `get_term`, `find_synonyms`, `validate_term`,
  `suggest_ontology`, `get_ancestors`, `get_descendants`, `map_across_ontologies`,
  `bulk_annotate`, `get_term_graph`
- FastAPI HTTP server with OpenAPI docs at `/docs`
- FastMCP server exposing all 10 tools to any MCP client (stdio + SSE)
- Jupyter extension: search panel, interactive term graph, `%%ontomcp` cell magic
- Support for 8 ontologies: GO, MONDO, HPO, ChEBI, UBERON, CL, EFO, MeSH
