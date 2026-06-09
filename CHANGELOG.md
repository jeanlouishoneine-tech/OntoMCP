# Changelog

All notable changes to OntoMCP are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
OntoMCP uses [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
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
