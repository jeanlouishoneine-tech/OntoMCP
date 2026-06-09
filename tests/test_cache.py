"""Unit tests for the SQLite cache layer. Pure SQLite — no network, no mocks."""

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
    # Rank-normalized score: best first, monotonically decreasing, in (0, 1].
    assert results[0]["score"] == 1.0
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 < s <= 1.0 for s in scores)


def test_rank_score_consistent_and_ordinal():
    from ontomcp.core.config import rank_score

    assert rank_score(0, 1) == 1.0
    assert rank_score(0, 5) == 1.0  # top hit is always 1.0 regardless of set size
    assert rank_score(4, 5) == 0.2
    assert rank_score(0, 0) == 0.0  # empty set guard
    # Strictly decreasing with position.
    five = [rank_score(i, 5) for i in range(5)]
    assert five == sorted(five, reverse=True)
    assert len(set(five)) == 5


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
