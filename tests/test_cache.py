"""Unit tests for the SQLite cache layer. Pure SQLite — no network, no mocks."""

import sqlite3
from pathlib import Path

import pytest

from ontomcp.core import cache


@pytest.fixture
def db(tmp_db_path: Path) -> Path:
    """An initialized, empty cache database."""
    cache.init_db(tmp_db_path)
    return tmp_db_path


def _term(curie="GO:0008219", label="cell death", definition="any process that ends life", **kw):
    base = {
        "curie": curie,
        "ontology": curie.split(":")[0],
        "label": label,
        "definition": definition,
        "is_obsolete": 0,
        "replaced_by": None,
        "raw_json": {"obo_id": curie},
        "synonyms": {"exact": ["cell killing"], "related": ["apoptosis-related"]},
    }
    base.update(kw)
    return base


# --- init / schema ---------------------------------------------------------


def test_init_db_idempotent(tmp_db_path: Path):
    cache.init_db(tmp_db_path)
    cache.init_db(tmp_db_path)  # second call must not raise
    conn = cache._connect(tmp_db_path)
    try:
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"terms", "synonyms", "relationships", "terms_fts", "ontology_versions"} <= names


def test_wal_mode_enabled(db: Path):
    conn = cache._connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_busy_timeout_set(db: Path):
    from ontomcp.core.config import BUSY_TIMEOUT_MS

    conn = cache._connect(db)
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert timeout == BUSY_TIMEOUT_MS


def test_busy_timeout_is_set_before_journal_mode(db: Path):
    """WAL's first activation on a fresh file itself needs a lock, so
    busy_timeout must be in effect before that pragma runs, not after — a
    connection racing another process's first WAL switch must not see
    `database is locked` with no timeout applied yet."""
    src = Path(cache.__file__).read_text()
    busy_at = src.index("PRAGMA busy_timeout")
    wal_at = src.index('PRAGMA journal_mode=WAL"')
    assert busy_at < wal_at


def test_read_retries_transient_lock_contention_then_succeeds(db: Path):
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = cache._read(db, flaky, default="fallback")
    assert result == "ok"
    assert calls["n"] == 3


def test_read_degrades_to_default_instead_of_raising_when_lock_never_clears(db: Path):
    def always_locked(conn):
        raise sqlite3.OperationalError("database is locked")

    result = cache._read(db, always_locked, default="fallback")
    assert result == "fallback"


# --- put / get round-trip --------------------------------------------------


def test_put_get_round_trip(db: Path):
    cache.put_term(db, _term())
    got = cache.get_term(db, "GO:0008219")
    assert got is not None
    assert got["curie"] == "GO:0008219"
    assert got["ontology"] == "GO"
    assert got["label"] == "cell death"
    assert got["raw_json"] == {"obo_id": "GO:0008219"}
    assert got["synonyms"]["exact"] == ["cell killing"]
    assert got["synonyms"]["related"] == ["apoptosis-related"]
    assert got["synonyms"]["narrow"] == []


def test_put_get_round_trip_new_provenance_columns(db: Path):
    cache.put_term(
        db,
        _term(
            consider=["GO:0006915"],
            subsets=["gocheck_do_not_annotate"],
            definition_sources=["GOC:mtg", "PMID:12345"],
            has_children=True,
            is_leaf=False,
        ),
    )
    got = cache.get_term(db, "GO:0008219")
    assert got["consider"] == ["GO:0006915"]
    assert got["subsets"] == ["gocheck_do_not_annotate"]
    assert got["definition_sources"] == ["GOC:mtg", "PMID:12345"]
    assert got["has_children"] is True
    assert got["is_leaf"] is False


def test_put_get_round_trip_optional_columns_default(db: Path):
    # A minimal term (no provenance fields) yields empty lists and None flags.
    cache.put_term(db, _term())
    got = cache.get_term(db, "GO:0008219")
    assert got["consider"] == []
    assert got["subsets"] == []
    assert got["definition_sources"] == []
    assert got["has_children"] is None
    assert got["is_leaf"] is None


def test_init_db_migrates_legacy_terms_table(tmp_db_path: Path):
    # Simulate a pre-upgrade cache: a terms table without the new columns.
    conn = cache._connect(tmp_db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE terms (
                    curie TEXT PRIMARY KEY, ontology TEXT NOT NULL, label TEXT NOT NULL,
                    definition TEXT, is_obsolete INTEGER DEFAULT 0, replaced_by TEXT,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, raw_json TEXT
                )
                """
            )
    finally:
        conn.close()

    cache.init_db(tmp_db_path)  # must add the missing columns, not raise

    conn = cache._connect(tmp_db_path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(terms)").fetchall()}
    finally:
        conn.close()
    assert {"consider", "subsets", "definition_sources", "has_children", "is_leaf"} <= cols


def _init_db_worker(path: str) -> str | None:
    """Top-level so it is picklable for multiprocessing's spawn context."""
    from ontomcp.core import cache as _cache

    try:
        _cache.init_db(path)
        return None
    except Exception as exc:  # noqa: BLE001 - report back to the parent process
        return repr(exc)


def test_init_db_survives_a_concurrent_dual_server_startup_race(tmp_path: Path):
    # FastMCP and FastAPI both call init_db() against the same file at startup.
    # Against a fresh (or pre-migration) DB, several processes racing the same
    # check-then-act ALTER TABLE / FTS-rebuild steps used to raise
    # `sqlite3.OperationalError: duplicate column name` in the losing processes.
    import multiprocessing as mp

    db_path = str(tmp_path / "concurrent.db")
    ctx = mp.get_context("spawn")
    with ctx.Pool(8) as pool:
        results = pool.map(_init_db_worker, [db_path] * 8)
    assert all(r is None for r in results), results

    conn = cache._connect(db_path)
    try:
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"terms", "synonyms", "relationships", "terms_fts", "ontology_versions"} <= names


def test_ontology_version_round_trip(db: Path):
    assert cache.get_ontology_version(db, "GO") is None
    cache.put_ontology_version(db, "GO", "2026-05-01")
    assert cache.get_ontology_version(db, "GO") == "2026-05-01"
    cache.put_ontology_version(db, "GO", "2026-06-01")  # idempotent upsert
    assert cache.get_ontology_version(db, "GO") == "2026-06-01"


def test_get_missing_returns_none(db: Path):
    assert cache.get_term(db, "GO:9999999") is None


def test_put_upsert_no_duplicates_and_replaces_synonyms(db: Path):
    cache.put_term(db, _term())
    cache.put_term(db, _term(label="cell death (updated)", synonyms={"exact": ["only one"]}))

    conn = cache._connect(db)
    try:
        n_terms = conn.execute("SELECT COUNT(*) FROM terms WHERE curie='GO:0008219'").fetchone()[0]
        n_syn = conn.execute("SELECT COUNT(*) FROM synonyms WHERE curie='GO:0008219'").fetchone()[0]
    finally:
        conn.close()

    assert n_terms == 1
    assert n_syn == 1  # old two synonyms replaced by one
    got = cache.get_term(db, "GO:0008219")
    assert got["label"] == "cell death (updated)"
    assert got["synonyms"]["exact"] == ["only one"]


def test_put_term_accepts_synonyms_as_pair_list(db: Path):
    """put_term must accept the (type, value) tuple-list form, not just a mapping."""
    cache.put_term(db, _term(synonyms=[("exact", "cell killing"), ("narrow", "apoptosis")]))
    got = cache.get_term(db, "GO:0008219")
    assert got["synonyms"]["exact"] == ["cell killing"]
    assert got["synonyms"]["narrow"] == ["apoptosis"]


# --- FTS search ------------------------------------------------------------


def test_fts_search_ranks_relevant_first(db: Path):
    cache.put_term(db, _term("GO:0008219", "cell death", "the process of cell death"))
    cache.put_term(db, _term("GO:0007049", "cell cycle", "the cell division cycle"))
    cache.put_term(db, _term("MONDO:0005233", "lung cancer", "a carcinoma of the lung"))

    results = cache.fts_search(db, "cell death")
    assert results
    assert results[0]["curie"] == "GO:0008219"
    # rank is the 1-based ordinal position, ascending. There is no score: a value
    # derived from result-set size alone made a weak three-row cache result and a
    # strong ten-row OLS result both report 1.0 for their top hit.
    assert results[0]["rank"] == 1
    assert [r["rank"] for r in results] == list(range(1, len(results) + 1))
    assert all("score" not in r for r in results)


def test_rank_score_is_gone():
    """The fake similarity is deleted, not merely unused."""
    from ontomcp.core import config

    assert not hasattr(config, "rank_score")


def test_fts_search_ontology_filter(db: Path):
    cache.put_term(db, _term("GO:0008219", "cell death"))
    cache.put_term(db, _term("MONDO:0005233", "cell death disorder", "disorder of cell death"))

    only_go = cache.fts_search(db, "cell death", ontologies=["GO"])
    assert {r["ontology"] for r in only_go} == {"GO"}

    assert cache.fts_search(db, "cell death", ontologies=["NOPE"]) == []


def test_fts_search_punctuation_does_not_raise(db: Path):
    cache.put_term(db, _term("GO:0008219", "cell-death", "cell-death process"))
    results = cache.fts_search(db, "cell-death")  # hyphen must not break MATCH
    assert isinstance(results, list)


def test_fts_search_replays_a_recorded_ols_order_over_local_bm25(db: Path):
    """A cache hit for a covered query must reproduce the order OLS originally gave.

    Local bm25 and OLS relevance are different algorithms; without replaying the
    recorded order, the same query silently returns a different #1 result
    depending on whether it is served from cache or from OLS.
    """
    # Insert in an order bm25 would NOT naturally reproduce (label match strength
    # favors GO:2 for "cell death", but OLS originally ranked GO:1 first).
    cache.put_term(db, _term("GO:1", "cell death", "cell death"))
    cache.put_term(db, _term("GO:2", "cell death process", "the process of cell death"))
    cache.put_term(db, _term("GO:3", "cell death regulation", "regulation of cell death"))

    cache.record_query(
        db, "cell death", "scope1", limit=10, row_count=3,
        ranked_curies=["GO:3", "GO:1", "GO:2"],
    )

    results = cache.fts_search(db, "cell death", query_norm="cell death", scope_key="scope1")
    assert [r["curie"] for r in results] == ["GO:3", "GO:1", "GO:2"]
    assert [r["rank"] for r in results] == [1, 2, 3]


def test_fts_search_ignores_recorded_order_for_a_different_scope(db: Path):
    cache.put_term(db, _term("GO:1", "cell death", "cell death"))
    cache.put_term(db, _term("GO:2", "cell death process", "the process of cell death"))
    cache.record_query(
        db, "cell death", "scope1", limit=10, row_count=2, ranked_curies=["GO:2", "GO:1"]
    )

    # No query_norm/scope_key passed: falls back to plain bm25, as before.
    results = cache.fts_search(db, "cell death")
    assert {r["curie"] for r in results} == {"GO:1", "GO:2"}


def test_fts_search_unranked_rows_sort_after_recorded_ones(db: Path):
    cache.put_term(db, _term("GO:1", "cell death", "cell death"))
    cache.put_term(db, _term("GO:2", "cell death process", "the process of cell death"))
    # Only GO:2 was part of the recorded OLS answer; GO:1 entered FTS some other way.
    cache.record_query(db, "cell death", "scope1", limit=10, row_count=1, ranked_curies=["GO:2"])

    results = cache.fts_search(db, "cell death", query_norm="cell death", scope_key="scope1")
    assert [r["curie"] for r in results][0] == "GO:2"


# --- relationships ---------------------------------------------------------


def test_relationships_dedupe_and_read(db: Path):
    cache.put_term(db, _term("GO:0008219", "cell death"))
    cache.put_term(db, _term("GO:0008150", "biological process"))
    cache.put_term(db, _term("GO:0070265", "necrotic cell death"))

    pairs = [
        ("GO:0008150", "GO:0008219", "is_a"),  # parent, child
        ("GO:0008219", "GO:0070265", "is_a"),
    ]
    cache.put_relationships(db, pairs)
    cache.put_relationships(db, pairs)  # repeat: must dedupe

    conn = cache._connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    finally:
        conn.close()
    assert count == 2

    ancestors = cache.get_ancestors_cached(db, "GO:0008219")
    assert ancestors == [{"curie": "GO:0008150", "label": "biological process", "rel_type": "is_a"}]

    descendants = cache.get_descendants_cached(db, "GO:0008219")
    assert descendants == [
        {"curie": "GO:0070265", "label": "necrotic cell death", "rel_type": "is_a"}
    ]


def test_relationship_label_none_when_term_uncached(db: Path):
    cache.put_term(db, _term("GO:0008219", "cell death"))
    cache.put_relationships(db, [("GO:0000001", "GO:0008219", "is_a")])
    ancestors = cache.get_ancestors_cached(db, "GO:0008219")
    assert ancestors == [{"curie": "GO:0000001", "label": None, "rel_type": "is_a"}]


# --- TTL / staleness -------------------------------------------------------


def test_is_stale_fresh_term(db: Path):
    cache.put_term(db, _term())
    assert cache.is_stale(db, "GO:0008219") is False


def test_is_stale_absent_term(db: Path):
    assert cache.is_stale(db, "GO:9999999") is True


def test_is_stale_old_term(db: Path):
    cache.put_term(db, _term())
    conn = cache._connect(db)
    try:
        with conn:
            conn.execute(
                "UPDATE terms SET fetched_at = datetime('now', '-30 days') WHERE curie = ?",
                ("GO:0008219",),
            )
    finally:
        conn.close()
    assert cache.is_stale(db, "GO:0008219") is True


# --- get_term_if_fresh (single-query cache+TTL on the hot path) -------------


def test_get_term_if_fresh_returns_fresh_term(db: Path):
    cache.put_term(db, _term())
    got = cache.get_term_if_fresh(db, "GO:0008219")
    assert got is not None
    assert got["curie"] == "GO:0008219"
    assert got["synonyms"]["exact"] == ["cell killing"]
    assert "fresh" not in got  # computed column must not leak into the payload


def test_get_term_if_fresh_none_when_absent(db: Path):
    assert cache.get_term_if_fresh(db, "GO:9999999") is None


def test_get_term_if_fresh_none_when_stale(db: Path):
    cache.put_term(db, _term())
    conn = cache._connect(db)
    try:
        with conn:
            conn.execute(
                "UPDATE terms SET fetched_at = datetime('now', '-30 days') WHERE curie = ?",
                ("GO:0008219",),
            )
    finally:
        conn.close()
    assert cache.get_term_if_fresh(db, "GO:0008219") is None


# --- concurrent writers (FastAPI + FastMCP share one DB) -------------------
# Both servers write through the shared core, so two OS processes can write at
# once. These tests prove WAL + busy_timeout + idempotent writes let them
# converge without "database is locked" errors. Workers are module-level so the
# spawn start method (macOS default) can pickle them.

_N_WRITES = 60


def _writer_terms(db_path_str: str, prefix: str) -> int:
    """Upsert the same N curies repeatedly from one process. Returns error count."""
    from ontomcp.core import cache as cache_mod

    db_path = Path(db_path_str)
    errors = 0
    for i in range(_N_WRITES):
        curie = f"GO:{i % 10:07d}"  # only 10 distinct curies -> heavy upsert contention
        try:
            cache_mod.put_term(
                db_path,
                {
                    "curie": curie,
                    "ontology": "GO",
                    "label": f"{prefix}-{i}",
                    "definition": None,
                },
            )
        except Exception:  # noqa: BLE001 - test asserts this never happens
            errors += 1
    return errors


def _writer_relationships(db_path_str: str) -> int:
    """Insert the same relationship triples repeatedly. Returns error count."""
    from ontomcp.core import cache as cache_mod

    db_path = Path(db_path_str)
    errors = 0
    for i in range(_N_WRITES):
        try:
            cache_mod.put_relationships(
                db_path, [(f"GO:{i % 10:07d}", f"GO:{(i + 1) % 10:07d}", "is_a")]
            )
        except Exception:  # noqa: BLE001
            errors += 1
    return errors


def test_concurrent_writers_no_lock_errors_and_converge(db: Path):
    import multiprocessing as mp

    ctx = mp.get_context("spawn")  # explicit: matches the real two-process setup
    with ctx.Pool(processes=4) as pool:
        results = [
            pool.apply_async(_writer_terms, (str(db), "a")),
            pool.apply_async(_writer_terms, (str(db), "b")),
            pool.apply_async(_writer_relationships, (str(db),)),
            pool.apply_async(_writer_relationships, (str(db),)),
        ]
        error_counts = [r.get(timeout=60) for r in results]

    assert error_counts == [0, 0, 0, 0], f"writers hit lock errors: {error_counts}"

    # Idempotent writes converge: 10 distinct terms, no duplicate rows.
    conn = cache._connect(db)
    try:
        n_terms = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        n_distinct = conn.execute("SELECT COUNT(DISTINCT curie) FROM terms").fetchone()[0]
        n_rels = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    finally:
        conn.close()
    assert n_terms == 10
    assert n_distinct == 10
    assert n_rels == 10  # 10 distinct (parent, child, is_a) triples, deduped


# --- search write-back must never damage a cached term (A4 / BL-04) ---------


def test_search_stub_never_guts_a_full_term(db):
    """A search result is four fields; a cached term is eighteen plus synonyms.

    Writing search results through put_term destroyed every full term it touched —
    nulling raw_json (which map_across_ontologies reads for curated xrefs),
    deleting the synonyms rows, dropping is_obsolete, and re-stamping fetched_at
    so the gutted row looked fresh for another seven days.
    """
    cache.put_term(
        db,
        {
            "curie": "GO:0008219",
            "ontology": "GO",
            "label": "cell death",
            "definition": "The full definition.",
            "is_obsolete": 1,
            "synonyms": {"exact": ["cell suicide"]},
            "raw_json": {"obo_xref": [{"database": "MESH", "id": "D003643"}]},
        },
    )

    # The stub carries synonyms of its own — a search doc's flat, unscoped bag,
    # which is a weaker observation than the term fetch's scoped one.
    cache.put_search_stub(
        db,
        [
            {
                "curie": "GO:0008219",
                "ontology": "GO",
                "label": "cell death",
                "synonyms": ["something the search made up"],
            }
        ],
    )

    term = cache.get_term(db, "GO:0008219")
    assert term["raw_json"] is not None, "raw_json survives — mapping depends on it"
    assert term["synonyms"]["exact"] == ["cell suicide"], "synonyms survive"

    conn = cache._connect(db)
    synonyms_text = conn.execute(
        "SELECT synonyms_text FROM terms WHERE curie='GO:0008219'"
    ).fetchone()[0]
    conn.close()
    assert synonyms_text == "cell suicide", "a stub must not overwrite scoped synonyms"
    assert term["definition"] == "The full definition.", "a definition-less stub keeps the old one"
    assert term["is_obsolete"] == 1
    assert "is_stub" not in term, "an internal integrity flag must not leak into payloads"

    conn = cache._connect(db)
    stub_flag = conn.execute("SELECT is_stub FROM terms WHERE curie='GO:0008219'").fetchone()[0]
    conn.close()
    assert stub_flag == 0, "a stub write must not demote a full term"


def test_get_term_if_fresh_refuses_a_stub(db):
    """The gate clause: a search followed by get_term returns a COMPLETE term."""
    cache.put_search_stub(db, [{"curie": "CHEBI:15377", "ontology": "CHEBI", "label": "water"}])
    # The stub is in FTS (so search is warmed) but must not satisfy a term lookup.
    assert cache.fts_search(db, "water")
    assert cache.get_term_if_fresh(db, "CHEBI:15377") is None

    cache.put_term(
        db,
        {
            "curie": "CHEBI:15377",
            "ontology": "CHEBI",
            "label": "water",
            "definition": "An oxygen hydride.",
            "synonyms": {"exact": ["H2O"]},
        },
    )
    full = cache.get_term_if_fresh(db, "CHEBI:15377")
    assert full is not None and full["synonyms"]["exact"] == ["H2O"]


def test_stub_does_not_extend_a_real_term_ttl(db):
    cache.put_term(db, {"curie": "GO:1", "ontology": "GO", "label": "a"})
    conn = cache._connect(db)
    with conn:
        conn.execute("UPDATE terms SET fetched_at = datetime('now', '-30 days') WHERE curie='GO:1'")
    conn.close()

    cache.put_search_stub(db, [{"curie": "GO:1", "ontology": "GO", "label": "a"}])
    assert cache.get_term_if_fresh(db, "GO:1") is None, "a stub must not refresh the TTL"


def test_fts_search_does_not_consult_the_builtin_registry(db):
    """Storage must not know what a registry is (A1 / BL-01).

    The deleted check narrowed every profile query to the built-in ten and still
    reported cache_hit=True — a silent partial truth.
    """
    cache.put_term(db, {"curie": "PO:0025034", "ontology": "PO", "label": "leaf"})
    hits = cache.fts_search(db, "leaf", ["PO"])
    assert [h["curie"] for h in hits] == ["PO:0025034"]


def test_fts_search_filter_is_case_insensitive(db):
    cache.put_term(db, {"curie": "PO:0025034", "ontology": "PO", "label": "leaf"})
    assert cache.fts_search(db, "leaf", ["po"])


# --- search coverage ledger -------------------------------------------------


def test_record_query_row_count_does_not_regress_on_a_smaller_limit(db):
    """A later, weaker (smaller-limit) observation must not shrink row_count.

    limit_n already takes MAX() across writes; row_count must track the same
    write that set that max, not whichever write happened most recently.
    """
    cache.record_query(db, "q", "scope1", limit=10, row_count=7)
    cache.record_query(db, "q", "scope1", limit=5, row_count=2)

    conn = cache._connect(db)
    try:
        row = conn.execute(
            "SELECT limit_n, row_count FROM search_queries WHERE query_norm = ? AND scope_key = ?",
            ("q", "scope1"),
        ).fetchone()
    finally:
        conn.close()

    assert row["limit_n"] == 10
    assert row["row_count"] == 7


def test_record_query_row_count_updates_on_an_equal_or_larger_limit(db):
    cache.record_query(db, "q", "scope1", limit=5, row_count=2)
    cache.record_query(db, "q", "scope1", limit=10, row_count=9)

    conn = cache._connect(db)
    try:
        row = conn.execute(
            "SELECT limit_n, row_count FROM search_queries WHERE query_norm = ? AND scope_key = ?",
            ("q", "scope1"),
        ).fetchone()
    finally:
        conn.close()

    assert row["limit_n"] == 10
    assert row["row_count"] == 9
