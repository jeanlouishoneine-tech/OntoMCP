"""SQLite cache layer: schema, read, write, FTS5 search.

The core library is the only place that touches SQLite. FastAPI owns writes;
FastMCP reads only. Every function opens its own connection (WAL + foreign keys
set on open), works, and closes — simplest and safe across the two server
processes.

Callers pass canonical CURIEs (uppercase prefix, e.g. ``GO:0008219``). The cache
stores whatever it is given; normalization is the caller's responsibility.
"""

import json
import sqlite3
from pathlib import Path

from ontomcp.core.config import BUSY_TIMEOUT_MS, CACHE_TTL_DAYS, ONTOLOGIES, rank_score

# Synonym buckets returned by get_term. Order matches the four OBO scopes.
_SYNONYM_TYPES = ("exact", "related", "narrow", "broad")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS terms (
    curie               TEXT PRIMARY KEY,
    ontology            TEXT NOT NULL,
    label               TEXT NOT NULL,
    definition          TEXT,
    is_obsolete         INTEGER DEFAULT 0,
    replaced_by         TEXT,
    consider            TEXT,
    subsets             TEXT,
    definition_sources  TEXT,
    has_children        INTEGER,
    is_leaf             INTEGER,
    fetched_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    raw_json            TEXT
);

CREATE TABLE IF NOT EXISTS synonyms (
    curie         TEXT NOT NULL,
    synonym       TEXT NOT NULL,
    synonym_type  TEXT NOT NULL,
    FOREIGN KEY (curie) REFERENCES terms(curie) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS relationships (
    parent_curie  TEXT NOT NULL,
    child_curie   TEXT NOT NULL,
    rel_type      TEXT NOT NULL,
    PRIMARY KEY (parent_curie, child_curie, rel_type)
);

CREATE TABLE IF NOT EXISTS ontology_versions (
    ontology    TEXT PRIMARY KEY,
    version     TEXT,
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_terms_ontology  ON terms(ontology);
CREATE INDEX IF NOT EXISTS idx_terms_label     ON terms(label);
CREATE INDEX IF NOT EXISTS idx_synonyms_curie  ON synonyms(curie);

CREATE VIRTUAL TABLE IF NOT EXISTS terms_fts USING fts5(
    curie UNINDEXED,
    label,
    definition,
    content='terms',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS terms_ai AFTER INSERT ON terms BEGIN
    INSERT INTO terms_fts(rowid, curie, label, definition)
    VALUES (new.rowid, new.curie, new.label, new.definition);
END;

CREATE TRIGGER IF NOT EXISTS terms_ad AFTER DELETE ON terms BEGIN
    INSERT INTO terms_fts(terms_fts, rowid, curie, label, definition)
    VALUES ('delete', old.rowid, old.curie, old.label, old.definition);
END;

CREATE TRIGGER IF NOT EXISTS terms_au AFTER UPDATE ON terms BEGIN
    INSERT INTO terms_fts(terms_fts, rowid, curie, label, definition)
    VALUES ('delete', old.rowid, old.curie, old.label, old.definition);
    INSERT INTO terms_fts(rowid, curie, label, definition)
    VALUES (new.rowid, new.curie, new.label, new.definition);
END;
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with WAL journaling, foreign keys, and a busy timeout.

    Both servers read and write through the shared core, so two processes can
    attempt writes at once. WAL lets readers and a single writer proceed
    concurrently; ``busy_timeout`` makes a second writer wait for the lock
    (up to BUSY_TIMEOUT_MS) instead of failing immediately with
    ``database is locked``. Writes are idempotent (upsert / insert-or-ignore),
    so concurrent writers converge.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


# Columns added to ``terms`` after the v1 schema. ``init_db`` adds any that are
# missing so an existing cache file upgrades in place (SQLite has no
# ``ADD COLUMN IF NOT EXISTS``, so we diff against PRAGMA table_info).
_TERMS_ADDED_COLUMNS = {
    "consider": "TEXT",
    "subsets": "TEXT",
    "definition_sources": "TEXT",
    "has_children": "INTEGER",
    "is_leaf": "INTEGER",
}


def _migrate_terms_columns(conn: sqlite3.Connection) -> None:
    """Add any missing post-v1 columns to ``terms`` (additive, non-destructive)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(terms)").fetchall()}
    for column, decl in _TERMS_ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE terms ADD COLUMN {column} {decl}")


def init_db(db_path: Path) -> None:
    """Create all tables, indexes, the FTS5 table, and sync triggers.

    Idempotent — safe to call on every startup. Also upgrades an existing cache
    file in place by adding any columns introduced after the original schema.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        with conn:
            conn.executescript(_SCHEMA)
            _migrate_terms_columns(conn)
    finally:
        conn.close()


def _group_synonyms(conn: sqlite3.Connection, curie: str) -> dict[str, list[str]]:
    """Return synonyms for a CURIE grouped into the four OBO scope buckets."""
    grouped: dict[str, list[str]] = {t: [] for t in _SYNONYM_TYPES}
    rows = conn.execute(
        "SELECT synonym, synonym_type FROM synonyms WHERE curie = ?", (curie,)
    ).fetchall()
    for row in rows:
        grouped.setdefault(row["synonym_type"], []).append(row["synonym"])
    return grouped


# The persisted columns of the terms table, in order. Used to project a row into
# the public term dict without leaking any computed columns (e.g. a freshness flag).
_TERM_COLUMNS = (
    "curie",
    "ontology",
    "label",
    "definition",
    "is_obsolete",
    "replaced_by",
    "consider",
    "subsets",
    "definition_sources",
    "has_children",
    "is_leaf",
    "fetched_at",
    "raw_json",
)

# Columns stored as JSON text but exposed as Python lists.
_JSON_LIST_COLUMNS = ("consider", "subsets", "definition_sources")
# Columns stored as 0/1/NULL but exposed as bool | None.
_OPT_BOOL_COLUMNS = ("has_children", "is_leaf")


def _row_to_term(conn: sqlite3.Connection, row: sqlite3.Row, curie: str) -> dict:
    """Build the public term dict from a ``terms`` row (parses raw_json, groups synonyms)."""
    term = {col: row[col] for col in _TERM_COLUMNS}
    term["raw_json"] = json.loads(term["raw_json"]) if term["raw_json"] else None
    for col in _JSON_LIST_COLUMNS:
        term[col] = json.loads(term[col]) if term[col] else []
    for col in _OPT_BOOL_COLUMNS:
        term[col] = None if term[col] is None else bool(term[col])
    term["synonyms"] = _group_synonyms(conn, curie)
    return term


def get_term(db_path: Path, curie: str) -> dict | None:
    """Return the cached term as a dict, or None if not cached.

    Expects a canonical CURIE. Synonyms are grouped into
    ``{exact, related, narrow, broad}`` lists; ``raw_json`` is parsed back to a
    dict when present.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM terms WHERE curie = ?", (curie,)).fetchone()
        if row is None:
            return None
        return _row_to_term(conn, row, curie)
    finally:
        conn.close()


def get_term_if_fresh(db_path: Path, curie: str, ttl_days: int = CACHE_TTL_DAYS) -> dict | None:
    """Return the cached term only if present and newer than ``ttl_days``, else None.

    Freshness is checked in SQL (``datetime('now', ...)``, matching the UTC
    ``CURRENT_TIMESTAMP`` written by put_term) within the same connection that
    reads the row — one open instead of a separate get_term + is_stale pair on the
    hot path.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT *, fetched_at >= datetime('now', ?) AS fresh FROM terms WHERE curie = ?",
            (f"-{int(ttl_days)} days", curie),
        ).fetchone()
        if row is None or not row["fresh"]:
            return None
        return _row_to_term(conn, row, curie)
    finally:
        conn.close()


def _normalize_synonyms(raw) -> list[tuple[str, str]]:
    """Coerce a synonyms value into (synonym_type, synonym) pairs.

    Accepts either a ``{type: [value, ...]}`` mapping or a list of
    ``(type, value)`` tuples.
    """
    if not raw:
        return []
    if isinstance(raw, dict):
        return [(syn_type, value) for syn_type, values in raw.items() for value in values]
    return [(syn_type, value) for syn_type, value in raw]


def put_term(db_path: Path, term_dict: dict) -> None:
    """Upsert a normalized term and replace its synonyms, atomically.

    ``term_dict`` carries already-parsed fields: ``curie``, ``ontology``,
    ``label``, and optionally ``definition``, ``is_obsolete``, ``replaced_by``,
    ``raw_json`` (dict or str), and ``synonyms`` (mapping or pair list). OLS
    parsing happens in the OLS client, not here.
    """
    curie = term_dict["curie"]
    raw_json = term_dict.get("raw_json")
    if isinstance(raw_json, (dict, list)):
        raw_json = json.dumps(raw_json)

    def _json_list(value):
        return json.dumps(value) if value else None

    def _opt_int(value):
        return None if value is None else int(bool(value))

    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO terms
                    (curie, ontology, label, definition, is_obsolete, replaced_by,
                     consider, subsets, definition_sources, has_children, is_leaf,
                     raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(curie) DO UPDATE SET
                    ontology           = excluded.ontology,
                    label              = excluded.label,
                    definition         = excluded.definition,
                    is_obsolete        = excluded.is_obsolete,
                    replaced_by        = excluded.replaced_by,
                    consider           = excluded.consider,
                    subsets            = excluded.subsets,
                    definition_sources = excluded.definition_sources,
                    has_children       = excluded.has_children,
                    is_leaf            = excluded.is_leaf,
                    raw_json           = excluded.raw_json,
                    fetched_at         = CURRENT_TIMESTAMP
                """,
                (
                    curie,
                    term_dict["ontology"],
                    term_dict["label"],
                    term_dict.get("definition"),
                    int(term_dict.get("is_obsolete", 0)),
                    term_dict.get("replaced_by"),
                    _json_list(term_dict.get("consider")),
                    _json_list(term_dict.get("subsets")),
                    _json_list(term_dict.get("definition_sources")),
                    _opt_int(term_dict.get("has_children")),
                    _opt_int(term_dict.get("is_leaf")),
                    raw_json,
                ),
            )
            conn.execute("DELETE FROM synonyms WHERE curie = ?", (curie,))
            pairs = _normalize_synonyms(term_dict.get("synonyms"))
            if pairs:
                conn.executemany(
                    "INSERT INTO synonyms (curie, synonym, synonym_type) VALUES (?, ?, ?)",
                    [(curie, value, syn_type) for syn_type, value in pairs],
                )
    finally:
        conn.close()


def put_relationships(db_path: Path, pairs: list[tuple[str, str, str]]) -> None:
    """Bulk-insert ``(parent_curie, child_curie, rel_type)`` triples.

    Duplicates are ignored via the composite primary key.
    """
    if not pairs:
        return
    conn = _connect(db_path)
    try:
        with conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO relationships
                    (parent_curie, child_curie, rel_type)
                VALUES (?, ?, ?)
                """,
                pairs,
            )
    finally:
        conn.close()


def get_ontology_version(db_path: Path, ontology: str) -> str | None:
    """Return the cached source version for an ontology, or None if unknown."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT version FROM ontology_versions WHERE ontology = ?", (ontology,)
        ).fetchone()
    finally:
        conn.close()
    return row["version"] if row else None


def get_all_ontology_versions(db_path: Path) -> dict[str, str | None]:
    """Return every cached ``{ontology: version}`` captured so far (may be empty)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT ontology, version FROM ontology_versions").fetchall()
    finally:
        conn.close()
    return {row["ontology"]: row["version"] for row in rows}


def put_ontology_version(db_path: Path, ontology: str, version: str | None) -> None:
    """Upsert the source version for an ontology (idempotent)."""
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO ontology_versions (ontology, version, fetched_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(ontology) DO UPDATE SET
                    version    = excluded.version,
                    fetched_at = CURRENT_TIMESTAMP
                """,
                (ontology, version),
            )
    finally:
        conn.close()


def _fts_query(query: str) -> str:
    """Quote each whitespace token so punctuation can't break FTS5 MATCH syntax."""
    tokens = query.split()
    return " ".join('"' + tok.replace('"', '""') + '"' for tok in tokens)


def fts_search(
    db_path: Path,
    query: str,
    ontologies: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text search over cached labels and definitions.

    Returns ``[{curie, label, ontology, definition, score}, ...]`` ranked best
    first by the FTS5 bm25 rank. ``score`` is the rank-normalized 0–1 value from
    ``config.rank_score`` (consistent with the OLS path), not the raw bm25 score.
    Unknown ontology codes in the filter are dropped.
    """
    match = _fts_query(query)
    if not match:
        return []

    sql = """
        SELECT t.curie, t.label, t.ontology, t.definition, t.is_obsolete,
               terms_fts.rank AS rank
        FROM terms_fts
        JOIN terms t ON t.rowid = terms_fts.rowid
        WHERE terms_fts MATCH ?
    """
    params: list = [match]

    if ontologies:
        valid = [o for o in ontologies if o in ONTOLOGIES]
        if not valid:
            return []
        placeholders = ", ".join("?" for _ in valid)
        sql += f" AND t.ontology IN ({placeholders})"
        params.extend(valid)

    sql += " ORDER BY terms_fts.rank LIMIT ?"
    params.append(limit)

    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    # Rows already arrive bm25-ranked (best first); report a rank-normalized score
    # so it matches the OLS path's scale rather than the raw bm25 value.
    total = len(rows)
    return [
        {
            "curie": row["curie"],
            "label": row["label"],
            "ontology": row["ontology"],
            "definition": row["definition"],
            "is_obsolete": bool(row["is_obsolete"]),
            "score": rank_score(i, total),
        }
        for i, row in enumerate(rows)
    ]


def get_ancestors_cached(db_path: Path, curie: str) -> list[dict]:
    """Return immediate parents of ``curie`` from the relationships table.

    ``label`` is None if the parent term itself is not cached yet. Multi-level
    depth assembly lives in the hierarchy tool, not here.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT r.parent_curie AS curie, t.label AS label, r.rel_type AS rel_type
            FROM relationships r
            LEFT JOIN terms t ON t.curie = r.parent_curie
            WHERE r.child_curie = ?
            """,
            (curie,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_descendants_cached(db_path: Path, curie: str) -> list[dict]:
    """Return immediate children of ``curie`` from the relationships table.

    ``label`` is None if the child term itself is not cached yet.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT r.child_curie AS curie, t.label AS label, r.rel_type AS rel_type
            FROM relationships r
            LEFT JOIN terms t ON t.curie = r.child_curie
            WHERE r.parent_curie = ?
            """,
            (curie,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def is_stale(db_path: Path, curie: str, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    """Return True if the term is absent or older than ``ttl_days``.

    Comparison runs in SQL against ``datetime('now', ...)`` so it matches the
    UTC ``CURRENT_TIMESTAMP`` written by put_term.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT fetched_at < datetime('now', ?) AS stale FROM terms WHERE curie = ?",
            (f"-{int(ttl_days)} days", curie),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return True
    return bool(row["stale"])
