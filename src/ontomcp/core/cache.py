"""SQLite cache layer: schema, read, write, FTS5 search.

The core library is the only place that touches SQLite. FastAPI owns writes;
FastMCP reads only. Every function opens its own connection (WAL + foreign keys
set on open), works, and closes — simplest and safe across the two server
processes.

Callers pass canonical CURIEs (uppercase prefix, e.g. ``GO:0008219``). The cache
stores whatever it is given; normalization is the caller's responsibility.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

from ontomcp.core.config import BUSY_TIMEOUT_MS, CACHE_TTL_DAYS

logger = logging.getLogger("ontomcp")

# Synonym buckets returned by get_term. Order matches the four OBO scopes.
_SYNONYM_TYPES = ("exact", "related", "narrow", "broad")

# One statement per tuple entry, executed individually (never via executescript(),
# which force-commits any pending transaction before it runs — that broke the
# BEGIN IMMEDIATE lock init_db takes around the whole migration, letting two
# processes race the same not-yet-migrated file).
_SCHEMA_STATEMENTS = (
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS synonyms (
        curie         TEXT NOT NULL,
        synonym       TEXT NOT NULL,
        synonym_type  TEXT NOT NULL,
        FOREIGN KEY (curie) REFERENCES terms(curie) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relationships (
        parent_curie  TEXT NOT NULL,
        child_curie   TEXT NOT NULL,
        rel_type      TEXT NOT NULL,
        PRIMARY KEY (parent_curie, child_curie, rel_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ontology_versions (
        ontology    TEXT PRIMARY KEY,
        version     TEXT,
        fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # Which (query, scope) pairs the cache has actually answered, and how many
    # rows it served. Without this, "FTS returned something" was treated as "the
    # cache covers this query" — so a partial result was served as complete for
    # seven days.
    """
    CREATE TABLE IF NOT EXISTS search_queries (
        query_norm     TEXT NOT NULL,
        scope_key      TEXT NOT NULL,
        limit_n        INTEGER NOT NULL,
        row_count      INTEGER NOT NULL,
        -- JSON list of CURIEs in the order OLS returned them for this (query,
        -- scope). Replayed on a cache hit so the answer does not silently
        -- reorder itself relative to bm25 the moment it is served from FTS
        -- instead of OLS.
        ranked_curies  TEXT,
        fetched_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (query_norm, scope_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_terms_ontology  ON terms(ontology)",
    "CREATE INDEX IF NOT EXISTS idx_terms_label     ON terms(label)",
    "CREATE INDEX IF NOT EXISTS idx_synonyms_curie  ON synonyms(curie)",
)

# The FTS declaration and its three sync triggers, kept apart from _SCHEMA because
# they are recreated wholesale by the migration below rather than created once.
#
# Two properties the original declaration lacked, and both were silently costing
# every cache hit an answer weaker than the network's:
#
#   tokenize='porter unicode61'  stemming, so "apoptotic" matches "apoptosis"
#   synonyms_text                the denormalized synonym bag, so a trivial name
#                                or acronym finds the term its label never mentions
#
# Without these, every ``cache_hit=True`` was a systematically worse answer than
# the OLS call it replaced — a downgrade with a latency win.
# Same one-statement-per-entry rule as _SCHEMA_STATEMENTS, for the same reason.
_FTS_SCHEMA_STATEMENTS = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS terms_fts USING fts5(
        curie UNINDEXED,
        label,
        definition,
        synonyms_text,
        content='terms',
        content_rowid='rowid',
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS terms_ai AFTER INSERT ON terms BEGIN
        INSERT INTO terms_fts(rowid, curie, label, definition, synonyms_text)
        VALUES (new.rowid, new.curie, new.label, new.definition, new.synonyms_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS terms_ad AFTER DELETE ON terms BEGIN
        INSERT INTO terms_fts(terms_fts, rowid, curie, label, definition, synonyms_text)
        VALUES ('delete', old.rowid, old.curie, old.label, old.definition, old.synonyms_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS terms_au AFTER UPDATE ON terms BEGIN
        INSERT INTO terms_fts(terms_fts, rowid, curie, label, definition, synonyms_text)
        VALUES ('delete', old.rowid, old.curie, old.label, old.definition, old.synonyms_text);
        INSERT INTO terms_fts(rowid, curie, label, definition, synonyms_text)
        VALUES (new.rowid, new.curie, new.label, new.definition, new.synonyms_text);
    END
    """,
)


def _migrate_fts(conn: sqlite3.Connection) -> bool:
    """Rebuild ``terms_fts`` if it predates the stemmed, synonym-indexed declaration.

    Returns True when a rebuild happened. Safe and lossless: ``terms_fts`` is an
    external-content table, so its data lives in ``terms``. Dropping and
    recreating it costs an index rebuild, never a cached term — which is what
    makes shipping this as a plain migration acceptable at all.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='terms_fts'"
    ).fetchone()
    if row is not None and "synonyms_text" in (row["sql"] or ""):
        return False

    if row is not None:
        for trigger in ("terms_ai", "terms_ad", "terms_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE terms_fts")

    for stmt in _FTS_SCHEMA_STATEMENTS:
        conn.execute(stmt)
    # Repopulate from the external content table — every cached term survives.
    conn.execute("INSERT INTO terms_fts(terms_fts) VALUES('rebuild')")
    return True


def _write(db_path: Path, action) -> bool:
    """Run a write inside a transaction, swallowing lock contention. Returns success.

    ``bulk_annotate`` runs ten concurrent searches over up to 500 terms with
    ``BUSY_TIMEOUT_MS`` as the only backstop, so a busy timeout is a realistic
    outcome, not a theoretical one. An unhandled exception out of a tool violates
    the project rule that tools never raise — and a cache write is best-effort by
    nature: losing one write-back costs a future cache hit, nothing more.
    """
    conn = _connect(db_path)
    try:
        with conn:
            action(conn)
        return True
    except sqlite3.Error as exc:
        # Broad on purpose: lock contention (OperationalError) is the expected
        # case, but a constraint violation (IntegrityError) is just as much a
        # best-effort write-back failure and must not escape as an unhandled
        # exception either — see the docstring above.
        logger.warning("cache write skipped (%s): %s", db_path, exc)
        return False
    finally:
        conn.close()


_READ_MAX_ATTEMPTS = 3
_READ_RETRY_DELAY_S = 0.1


def _read(db_path: Path, action, default):
    """Run a read with a short bounded retry on lock contention, else ``default``.

    WAL's readers-never-blocked-by-a-writer guarantee does not cover the
    schema-altering DDL ``init_db`` runs on a startup/migration race (``ALTER
    TABLE``, an FTS rebuild) — that takes a stronger lock a concurrent reader can
    still see as ``database is locked``. A short retry rides out that narrow
    window instead of surfacing an unhandled exception; on exhaustion, degrading
    to ``default`` matches the meaning every caller already gives a cache miss —
    a fall-through to a live OLS fetch, not a crash.
    """
    conn = _connect(db_path)
    try:
        for attempt in range(_READ_MAX_ATTEMPTS):
            try:
                return action(conn)
            except sqlite3.OperationalError as exc:
                if attempt == _READ_MAX_ATTEMPTS - 1:
                    logger.warning("cache read skipped (%s): %s", db_path, exc)
                    return default
                time.sleep(_READ_RETRY_DELAY_S)
    finally:
        conn.close()


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
    # busy_timeout FIRST: switching a fresh file to WAL mode itself briefly needs
    # a stronger lock, and several processes racing that switch on first connect
    # would otherwise see `database is locked` immediately — this connection's
    # busy_timeout does not apply to a pragma executed before it is set.
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
    # The term's own IRI as OLS reports it. Authoritative — it is the one IRI we
    # never have to guess, and it is rung 1 of the IRI resolution chain in
    # core/iri.py (see candidates() / fetch_with_disconfirmation()).
    "iri": "TEXT",
    # 1 when the row came from a search result rather than a full term fetch. A
    # stub carries label/definition/iri and nothing else, so get_term must never
    # satisfy a lookup from one.
    "is_stub": "INTEGER NOT NULL DEFAULT 0",
    # Denormalized synonym bag, written by put_term from the synonyms it already
    # persists. Denormalized because FTS5 external-content tables index one table:
    # a join would need a view, and a view cannot be an FTS content source.
    "synonyms_text": "TEXT",
}


def _migrate_terms_columns(conn: sqlite3.Connection) -> None:
    """Add any missing post-v1 columns to ``terms`` (additive, non-destructive)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(terms)").fetchall()}
    for column, decl in _TERMS_ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE terms ADD COLUMN {column} {decl}")


def _migrate_search_queries_columns(conn: sqlite3.Connection) -> None:
    """Add ``ranked_curies`` to ``search_queries`` if a pre-existing db lacks it."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(search_queries)").fetchall()}
    if "ranked_curies" not in existing:
        conn.execute("ALTER TABLE search_queries ADD COLUMN ranked_curies TEXT")


# FastMCP and FastAPI both call init_db() at startup against the same file, so a
# fresh or pre-migration DB can have two processes racing the same check-then-act
# ALTER TABLE / FTS-rebuild steps. A few bounded, short-backed-off retries absorb
# the busy window created by BEGIN IMMEDIATE below before giving up for good.
_INIT_DB_MAX_ATTEMPTS = 3
_INIT_DB_RETRY_DELAY_S = 0.5


def init_db(db_path: Path) -> None:
    """Create all tables, indexes, the FTS5 table, and sync triggers.

    Idempotent — safe to call on every startup, including from two processes at
    once. The whole migration runs inside one ``BEGIN IMMEDIATE`` transaction:
    the loser of a startup race blocks on the lock (``BUSY_TIMEOUT_MS``) rather
    than racing the check-then-act ALTER TABLE / FTS-rebuild steps, and finds
    everything already migrated — a no-op — once it gets the lock. Also upgrades
    an existing cache file in place by adding any columns introduced after the
    original schema.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_INIT_DB_MAX_ATTEMPTS):
        conn = None
        try:
            # Connecting itself can race: switching a brand-new file to WAL mode
            # needs a stronger lock, so with several processes opening the same
            # not-yet-created file at once, _connect() can raise
            # `database is locked` before BEGIN IMMEDIATE is ever reached. That
            # must be retried too, not just the migration below.
            conn = _connect(db_path)
            conn.execute("BEGIN IMMEDIATE")
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)
            # Column migration first: the FTS declaration references
            # synonyms_text, so the column must exist before the virtual
            # table is created.
            _migrate_terms_columns(conn)
            _migrate_search_queries_columns(conn)
            if _migrate_fts(conn):
                logger.info("Rebuilt terms_fts with stemming and synonym indexing (%s)", db_path)
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.rollback()
            if attempt == _INIT_DB_MAX_ATTEMPTS - 1:
                raise
            logger.warning("init_db migration lock contention (%s), retrying: %s", db_path, exc)
            time.sleep(_INIT_DB_RETRY_DELAY_S)
        finally:
            if conn is not None:
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
    "iri",
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

    def _do(conn):
        row = conn.execute("SELECT * FROM terms WHERE curie = ?", (curie,)).fetchone()
        if row is None:
            return None
        return _row_to_term(conn, row, curie)

    return _read(db_path, _do, None)


def get_term_if_fresh(db_path: Path, curie: str, ttl_days: int = CACHE_TTL_DAYS) -> dict | None:
    """Return the cached term only if present, complete, and newer than ``ttl_days``.

    Freshness is checked in SQL (``datetime('now', ...)``, matching the UTC
    ``CURRENT_TIMESTAMP`` written by put_term) within the same connection that
    reads the row — one open instead of a separate get_term + is_stale pair on the
    hot path.

    ``is_stub = 0`` is part of the WHERE clause, not an afterthought: a search
    result warms the FTS index with a label-and-definition shell, and answering
    ``get_term`` from one would return a term with no synonyms, no xrefs and no
    obsolescence status while reporting ``cache_hit=True``.
    """

    def _do(conn):
        row = conn.execute(
            "SELECT *, fetched_at >= datetime('now', ?) AS fresh FROM terms "
            "WHERE curie = ? AND is_stub = 0",
            (f"-{int(ttl_days)} days", curie),
        ).fetchone()
        if row is None or not row["fresh"]:
            return None
        return _row_to_term(conn, row, curie)

    return _read(db_path, _do, None)


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


def _synonyms_text(values) -> str | None:
    """Flatten synonym strings into one searchable blob, deduped, order preserved.

    Takes a plain iterable of strings so both writers share it: ``put_term`` has
    scoped pairs and flattens them first, while a search result carries a flat
    list. OLS returns duplicates in its ``synonym`` field (21 entries for
    CHEBI:15365, several repeated), so deduping here is load-bearing, not tidiness.
    """
    seen: list[str] = []
    for value in values or ():
        if value and value not in seen:
            seen.append(value)
    return " ; ".join(seen) or None


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

    pairs = _normalize_synonyms(term_dict.get("synonyms"))

    def _do(conn):
        conn.execute(
            """
                INSERT INTO terms
                    (curie, ontology, label, definition, is_obsolete, replaced_by,
                     consider, subsets, definition_sources, has_children, is_leaf,
                     iri, is_stub, synonyms_text, raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, CURRENT_TIMESTAMP)
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
                    iri                = COALESCE(excluded.iri, terms.iri),
                    is_stub            = 0,
                    synonyms_text      = excluded.synonyms_text,
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
                term_dict.get("iri"),
                _synonyms_text([value for _scope, value in pairs]),
                raw_json,
            ),
        )
        conn.execute("DELETE FROM synonyms WHERE curie = ?", (curie,))
        if pairs:
            conn.executemany(
                "INSERT INTO synonyms (curie, synonym, synonym_type) VALUES (?, ?, ?)",
                [(curie, value, syn_type) for syn_type, value in pairs],
            )

    _write(db_path, _do)


def put_search_stub(db_path: Path, rows: list[dict]) -> None:
    """Warm the FTS index from search results without damaging any cached term.

    A search result carries four or five fields; a cached term carries eighteen
    plus its synonyms. Writing search results through ``put_term`` therefore
    *destroyed* every full term it touched — nulling ``raw_json`` (which
    ``map_across_ontologies`` reads for curated xrefs), deleting the synonyms
    rows, dropping ``is_obsolete``, and re-stamping ``fetched_at`` so the gutted
    row looked fresh for another seven days.

    This upsert is deliberately narrow:

    - it never writes ``raw_json`` and never deletes synonyms;
    - it does not touch ``fetched_at`` on an existing row, so a stub can never
      extend a real term's TTL;
    - **against a fully-fetched row it only fills in a missing ``iri``.** A search
      result is a weaker observation than a term fetch, so it may refresh another
      stub but must never contradict a full record — a search doc that omits
      ``is_obsolete`` would otherwise clear a real obsolescence flag;
    - ``is_stub`` stays 1 only for rows that were never fully fetched, so
      ``get_term`` can refuse to satisfy a lookup with a shell.

    It **does** carry ``synonyms_text``. Without it a search-warmed row is
    synonym-blind in FTS, so a synonym query could never be served from cache
    until someone happened to call ``get_term`` on that CURIE — which stranded the
    whole point of the synonym rebuild on one workflow. Synonyms obey the same
    stub-never-overwrites-a-full-row rule as every other field here: a term fetch
    sees scoped synonyms that a search doc does not.
    """
    if not rows:
        return

    def _do(conn):
        conn.executemany(
            """
                INSERT INTO terms
                    (curie, ontology, label, definition, is_obsolete, iri,
                     synonyms_text, is_stub, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(curie) DO UPDATE SET
                    label         = CASE WHEN terms.is_stub = 1
                                         THEN excluded.label ELSE terms.label END,
                    definition    = CASE WHEN terms.is_stub = 1
                                         THEN COALESCE(excluded.definition, terms.definition)
                                         ELSE terms.definition END,
                    is_obsolete   = CASE WHEN terms.is_stub = 1
                                         THEN excluded.is_obsolete ELSE terms.is_obsolete END,
                    synonyms_text = CASE WHEN terms.is_stub = 1
                                         THEN COALESCE(excluded.synonyms_text,
                                                       terms.synonyms_text)
                                         ELSE terms.synonyms_text END,
                    iri           = COALESCE(terms.iri, excluded.iri)
                """,
            [
                (
                    row["curie"],
                    row["ontology"],
                    row["label"],
                    row.get("definition"),
                    int(bool(row.get("is_obsolete", False))),
                    row.get("iri"),
                    _synonyms_text(row.get("synonyms")),
                )
                for row in rows
                if row.get("curie") and row.get("label")
            ],
        )

    _write(db_path, _do)


def put_relationships(db_path: Path, pairs: list[tuple[str, str, str]]) -> None:
    """Bulk-insert ``(parent_curie, child_curie, rel_type)`` triples.

    Duplicates are ignored via the composite primary key.
    """
    if not pairs:
        return
    _write(
        db_path,
        lambda conn: conn.executemany(
            """
            INSERT OR IGNORE INTO relationships (parent_curie, child_curie, rel_type)
            VALUES (?, ?, ?)
            """,
            pairs,
        ),
    )


def get_ontology_version(db_path: Path, ontology: str) -> str | None:
    """Return the cached source version for an ontology, or None if unknown."""

    def _do(conn):
        row = conn.execute(
            "SELECT version FROM ontology_versions WHERE ontology = ?", (ontology,)
        ).fetchone()
        return row["version"] if row else None

    return _read(db_path, _do, None)


def get_ontology_version_if_fresh(db_path: Path, ontology: str, ttl_days: int) -> str | None:
    """Return the cached version only if it was observed within ``ttl_days``.

    ``ontology_versions.fetched_at`` was written on every upsert and read nowhere,
    so a version captured once was reused forever — the tool reported a release
    string from an arbitrary past date as though it were current. Versions age far
    more slowly than terms (hence VERSION_TTL_DAYS, not CACHE_TTL_DAYS) but they do
    age.
    """

    def _do(conn):
        row = conn.execute(
            "SELECT version FROM ontology_versions "
            "WHERE ontology = ? AND fetched_at >= datetime('now', ?)",
            (ontology, f"-{int(ttl_days)} days"),
        ).fetchone()
        return row["version"] if row else None

    return _read(db_path, _do, None)


def get_all_ontology_versions(db_path: Path) -> dict[str, str | None]:
    """Return every cached ``{ontology: version}`` captured so far (may be empty)."""

    def _do(conn):
        rows = conn.execute("SELECT ontology, version FROM ontology_versions").fetchall()
        return {row["ontology"]: row["version"] for row in rows}

    return _read(db_path, _do, {})


def put_ontology_version(db_path: Path, ontology: str, version: str | None) -> None:
    """Upsert the source version for an ontology (idempotent)."""
    _write(
        db_path,
        lambda conn: conn.execute(
            """
            INSERT INTO ontology_versions (ontology, version, fetched_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(ontology) DO UPDATE SET
                version    = excluded.version,
                fetched_at = CURRENT_TIMESTAMP
            """,
            (ontology, version),
        ),
    )


def _fts_query(query: str) -> str:
    """Quote each whitespace token so punctuation can't break FTS5 MATCH syntax.

    The final token gets a ``*`` so a partially typed word still matches: a
    scientist searching "apopto" means "apoptosis". Only the last token, because
    prefix-matching an interior token widens the match set without helping — the
    user finished typing those.
    """
    tokens = [tok.replace('"', '""') for tok in query.split()]
    if not tokens:
        return ""
    quoted = ['"' + tok + '"' for tok in tokens[:-1]]
    quoted.append('"' + tokens[-1] + '"*')
    return " ".join(quoted)


def scope_key(scope: list[str] | None, frozen_at: str | None) -> str:
    """Identity of a search scope, for the coverage ledger.

    Includes ``frozen_at`` so a ``profile refresh`` is not ignored for seven days:
    the same query over the same-looking member list is a *different* question once
    the profile has been re-frozen against a new index.
    """
    import hashlib

    body = ",".join(sorted(scope or [])) + "|" + (frozen_at or "")
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def query_is_covered(
    db_path: Path,
    query_norm: str,
    key: str,
    limit: int,
    fts_rows: int,
    ttl_days: int = CACHE_TTL_DAYS,
) -> bool:
    """Has this exact (query, scope) been answered from OLS, and does FTS still hold it?

    This replaces ``if hits: return hits``, which treated *any* FTS row as proof
    the cache covered the query — so a three-row partial answer was served as
    complete for seven days.

    It deliberately does **not** use ``len(hits) >= limit``, the obvious
    alternative: that is unsatisfiable forever for any query with fewer than
    ``limit`` genuinely matching terms, which turns cache-first into "check
    SQLite, then always call OLS".

    Serve from cache only when a fresh ledger row exists whose recorded limit is at
    least the requested one **and** FTS still returns at least as many rows as were
    served then.
    """

    def _do(conn):
        row = conn.execute(
            """
            SELECT limit_n, row_count FROM search_queries
            WHERE query_norm = ? AND scope_key = ? AND fetched_at >= datetime('now', ?)
            """,
            (query_norm, key, f"-{int(ttl_days)} days"),
        ).fetchone()
        if row is None:
            return False
        # row_count == 0 must never count as coverage: min(0, limit) == 0 makes
        # fts_rows >= 0 vacuously true, so a genuinely empty OLS answer would be
        # served as "covered" forever — permanently suppressing the escape
        # hatch, which only ever fires on the OLS branch.
        return (
            row["row_count"] > 0
            and row["limit_n"] >= limit
            and fts_rows >= min(row["row_count"], limit)
        )

    return _read(db_path, _do, False)


def record_query(
    db_path: Path,
    query_norm: str,
    key: str,
    limit: int,
    row_count: int,
    ranked_curies: list[str] | None = None,
) -> None:
    """Record that OLS answered this (query, scope), with how many rows and in what order.

    ``ranked_curies`` is replayed by ``fts_search`` on a subsequent cache hit for the
    same (query, scope), so the answer does not silently reorder itself relative to
    local bm25 the moment it is served from FTS instead of OLS.
    """
    ranked_json = json.dumps(ranked_curies) if ranked_curies else None
    _write(
        db_path,
        lambda conn: conn.execute(
            """
            INSERT INTO search_queries
                (query_norm, scope_key, limit_n, row_count, ranked_curies, fetched_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(query_norm, scope_key) DO UPDATE SET
                limit_n       = MAX(excluded.limit_n, search_queries.limit_n),
                row_count     = CASE WHEN excluded.limit_n >= search_queries.limit_n
                                   THEN excluded.row_count
                                   ELSE search_queries.row_count END,
                ranked_curies = CASE WHEN excluded.limit_n >= search_queries.limit_n
                                   THEN excluded.ranked_curies
                                   ELSE search_queries.ranked_curies END,
                fetched_at    = CURRENT_TIMESTAMP
            """,
            (query_norm, key, limit, row_count, ranked_json),
        ),
    )


def _row_dict(row: sqlite3.Row) -> dict:
    return {
        "curie": row["curie"],
        "label": row["label"],
        "ontology": row["ontology"],
        "definition": row["definition"],
        "is_obsolete": bool(row["is_obsolete"]),
        "iri": row["iri"],
    }


def fts_search(
    db_path: Path,
    query: str,
    ontologies: list[str] | None = None,
    limit: int = 10,
    *,
    query_norm: str | None = None,
    scope_key: str | None = None,
) -> list[dict]:
    """Full-text search over cached labels and definitions.

    Returns ``[{curie, label, ontology, definition, iri, rank}, ...]`` ranked best
    first. Matching covers labels, definitions and synonyms, with Porter stemming.

    When ``query_norm`` and ``scope_key`` are given, this replays the order OLS
    returned the last time it answered this exact (query, scope) — recorded by
    ``record_query`` — instead of the local FTS5 bm25 rank: the recorded CURIEs
    are looked up directly (not re-derived through a bm25-LIMIT'ed MATCH, which
    over-fetch can fill with rows bm25 favors over the ones actually returned
    last time) and placed first, in their recorded order. Any shortfall — a
    recorded CURIE aged out of cache, or no record at all — is padded with
    bm25-ordered rows not already included, up to ``limit``.
    """
    match = _fts_query(query)
    if not match:
        return []

    ontology_filter = ""
    ontology_params: list = []
    if ontologies:
        # No registry check here. Storage must not know what a registry is: the
        # caller (normalize_ontologies) has already validated and canonicalized
        # these codes, and the check that used to live here silently narrowed
        # every profile query to the built-in eleven and still reported
        # cache_hit=True. Stored ``terms.ontology`` is uppercase because
        # normalize_curie uppercases before the CURIE becomes the primary key
        # (see tournament/spike-results.md S-6), so uppercasing here is enough.
        codes = [o.upper() for o in ontologies]
        ontology_filter = f" AND ontology IN ({', '.join('?' for _ in codes)})"
        ontology_params = codes

    def _do(conn):
        ranked_curies: list[str] | None = None
        if query_norm is not None and scope_key is not None:
            ledger_row = conn.execute(
                "SELECT ranked_curies FROM search_queries WHERE query_norm = ? AND scope_key = ?",
                (query_norm, scope_key),
            ).fetchone()
            if ledger_row is not None and ledger_row["ranked_curies"]:
                ranked_curies = json.loads(ledger_row["ranked_curies"])

        results: list[dict] = []
        seen: set[str] = set()
        if ranked_curies:
            placeholders = ", ".join("?" for _ in ranked_curies)
            rows_by_curie = {
                row["curie"]: row
                for row in conn.execute(
                    f"SELECT curie, label, ontology, definition, is_obsolete, iri "
                    f"FROM terms WHERE curie IN ({placeholders}){ontology_filter}",
                    [*ranked_curies, *ontology_params],
                ).fetchall()
            }
            for curie in ranked_curies:
                row = rows_by_curie.get(curie)
                if row is not None:
                    results.append(_row_dict(row))
                    seen.add(curie)

        if len(results) < limit:
            sql = (
                "SELECT t.curie, t.label, t.ontology, t.definition, t.is_obsolete, t.iri "
                "FROM terms_fts JOIN terms t ON t.rowid = terms_fts.rowid "
                "WHERE terms_fts MATCH ?" + ontology_filter + " ORDER BY terms_fts.rank LIMIT ?"
            )
            # Over-fetch enough to survive excluding already-included rows.
            pad_limit = limit + len(seen)
            for row in conn.execute(sql, [match, *ontology_params, pad_limit]).fetchall():
                if len(results) >= limit:
                    break
                if row["curie"] not in seen:
                    results.append(_row_dict(row))
                    seen.add(row["curie"])
        return results

    results = _read(db_path, _do, [])
    results = results[:limit]
    # Report the ordinal position, not a normalized score: the old rank_score
    # was a function of result-set size alone, so a weak three-row cache result
    # and a strong ten-row OLS result both reported 1.0 for their top hit.
    for i, row in enumerate(results, start=1):
        row["rank"] = i
    return results


def get_ancestors_cached(db_path: Path, curie: str) -> list[dict]:
    """Return immediate parents of ``curie`` from the relationships table.

    ``label`` is None if the parent term itself is not cached yet. Multi-level
    depth assembly lives in the hierarchy tool, not here.
    """

    def _do(conn):
        rows = conn.execute(
            """
            SELECT r.parent_curie AS curie, t.label AS label, r.rel_type AS rel_type
            FROM relationships r
            LEFT JOIN terms t ON t.curie = r.parent_curie
            WHERE r.child_curie = ?
            """,
            (curie,),
        ).fetchall()
        return [dict(row) for row in rows]

    return _read(db_path, _do, [])


def get_descendants_cached(db_path: Path, curie: str) -> list[dict]:
    """Return immediate children of ``curie`` from the relationships table.

    ``label`` is None if the child term itself is not cached yet.
    """

    def _do(conn):
        rows = conn.execute(
            """
            SELECT r.child_curie AS curie, t.label AS label, r.rel_type AS rel_type
            FROM relationships r
            LEFT JOIN terms t ON t.curie = r.child_curie
            WHERE r.parent_curie = ?
            """,
            (curie,),
        ).fetchall()
        return [dict(row) for row in rows]

    return _read(db_path, _do, [])


def is_stale(db_path: Path, curie: str, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    """Return True if the term is absent or older than ``ttl_days``.

    Comparison runs in SQL against ``datetime('now', ...)`` so it matches the
    UTC ``CURRENT_TIMESTAMP`` written by put_term.
    """

    def _do(conn):
        row = conn.execute(
            "SELECT fetched_at < datetime('now', ?) AS stale FROM terms WHERE curie = ?",
            (f"-{int(ttl_days)} days", curie),
        ).fetchone()
        return True if row is None else bool(row["stale"])

    return _read(db_path, _do, True)
