"""Retrieval evaluation — the non-regression gate for Phases C and D.

**There are no absolute thresholds.** No baseline existed when this plan was
written, so any `recall@10 >= 0.90` would have been a number picked out of the
air — the same objection that killed the flood-share gate. The gate is instead:
**no fixture query regresses against a baseline captured on the same fixture
before the phase's changes landed.**

The baseline lives at ``tests/fixtures/retrieval_baseline.json`` and is captured
against live OLS with::

    uv run pytest -m integration tests/test_retrieval.py --capture-baseline

Marked integration throughout: it hits OLS, so ``pytest -m "not integration"``
skips it and offline development is unaffected.
"""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GOLD = FIXTURES / "retrieval_gold.jsonl"
BASELINE = FIXTURES / "retrieval_baseline.json"

# Which ontologies each starter searches by default. Kept here rather than read
# from src/ontomcp/starters so the eval measures retrieval, not starter loading.
PROFILES = {
    "pharma": ["CHEBI", "NCIT", "MONDO", "HPO", "GO"],
    "biotech-molecular": ["GO", "SO", "CL", "UBERON", "CLO", "CHEBI"],
    "plant-breeding": ["PO", "TO", "PECO", "GO", "ENVO", "NCBITAXON", "UO"],
    "fermentation": ["NCBITAXON", "CHEBI", "GO", "ENVO", "OMP", "FOODON", "UO", "CHMO"],
    "clinical-rare-disease": ["MONDO", "HPO", "ORDO", "SO", "GENO", "NCIT", "UO"],
    "microbiome": ["NCBITAXON", "CHEBI", "GO", "ENVO", "OMP", "UBERON", "MONDO"],
}


def load_gold() -> list[dict]:
    return [json.loads(line) for line in GOLD.read_text().splitlines() if line.strip()]


def _normalize(curie: str) -> str:
    """Compare CURIEs the way the cache keys them (HP -> HPO, uppercase prefix)."""
    from ontomcp.core.ols_client import normalize_curie

    try:
        return normalize_curie(curie)
    except ValueError:
        return curie.upper()


def score_one(row: dict, results: list[dict]) -> dict:
    """Metrics for a single query. Reported per query so a regression names itself."""
    gold = {_normalize(c) for c in row["gold"]}
    found = [_normalize(r.get("curie", "")) for r in results if r.get("curie")]
    top10 = found[:10]

    # `gold` is a set of ACCEPTABLE answers, not a set of required ones, so the
    # metric is any-of (hit@10) rather than all-of (recall@10). An all-of metric
    # demands finding every synonym-equivalent CURIE, which nobody wants: "Marfan
    # syndrome" finding only ORDO:558 would score 0.5 despite being right.
    #
    # Set membership, not a list count: OLS returns the same CURIE more than once
    # (imported copies), and a metric must not inherit the defect it measures.
    hit = 1.0 if gold & set(top10) else 0.0
    reciprocal = 0.0
    for position, curie in enumerate(top10, start=1):
        if curie in gold:
            reciprocal = 1.0 / position
            break

    scope = set(PROFILES[row["profile"]])
    out_of_domain = [c for c in top10 if c.split(":", 1)[0] not in scope]

    shares: dict[str, int] = {}
    for curie in top10:
        prefix = curie.split(":", 1)[0]
        shares[prefix] = shares.get(prefix, 0) + 1

    return {
        "hit@10": hit,
        "mrr@10": round(reciprocal, 3),
        "out_of_domain@10": round(len(out_of_domain) / len(top10), 3) if top10 else 0.0,
        "max_ontology_share@10": round(max(shares.values()) / len(top10), 3) if top10 else 0.0,
        "found": top10,
    }


async def run_eval() -> dict:
    """Run every gold query through ``search_terms`` — the actual product tool
    a caller invokes, not the raw OLS client — and return ``{query_key: metrics}``.

    Routing through ``search_terms`` (rather than ``OLSClient.search`` directly)
    is what makes this a regression gate on the product: it exercises the same
    dedup (``_collapse``), minting-prefix filter, and over-fetch logic the code
    under review changes, so a regression in any of those actually shows up here.
    A raw-OLS baseline cannot regress against changes to code it never calls.
    """
    import os
    import tempfile
    from pathlib import Path as _Path

    from ontomcp.core import cache
    from ontomcp.core.ols_client import OLSClient
    from ontomcp.core.tools.search import search_terms

    # search_terms rejects any ontology not in the ACTIVE profile's scope, so each
    # query must run with its own starter profile active (PROFILES' keys are the
    # shipped starter names) — otherwise every profile but whichever is ambient at
    # test time gets rejected as out_of_profile before it ever reaches OLS.
    prev_profile = os.environ.get("ONTOMCP_PROFILE")

    report: dict[str, dict] = {}
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = _Path(tmp_dir) / "retrieval_eval.db"
            cache.init_db(db_path)
            async with OLSClient() as cli:
                for row in load_gold():
                    os.environ["ONTOMCP_PROFILE"] = row["profile"]
                    key = f"{row['profile']}::{row['query']}"
                    envelope, _ = await search_terms(
                        row["query"],
                        PROFILES[row["profile"]],
                        limit=10,
                        db_path=db_path,
                        client=cli,
                    )
                    if isinstance(envelope, list):
                        pytest.skip(f"OLS unavailable: {envelope}")
                    report[key] = score_one(row, envelope["results"])
    finally:
        if prev_profile is None:
            os.environ.pop("ONTOMCP_PROFILE", None)
        else:
            os.environ["ONTOMCP_PROFILE"] = prev_profile
    return report


# --- offline structural checks (always run) --------------------------------


def test_fixture_covers_every_starter_but_omics():
    """30 rows, five per starter. omics is covered by the compatibility test instead."""
    rows = load_gold()
    assert len(rows) == 30
    by_profile: dict[str, int] = {}
    for row in rows:
        by_profile[row["profile"]] = by_profile.get(row["profile"], 0) + 1
    assert by_profile == dict.fromkeys(PROFILES, 5)
    assert "omics" not in by_profile


def test_every_gold_curie_is_from_an_in_scope_ontology():
    """A gold CURIE the profile cannot search is an un-passable test, not a hard one."""
    for row in load_gold():
        scope = set(PROFILES[row["profile"]])
        for curie in row["gold"]:
            prefix = _normalize(curie).split(":", 1)[0]
            assert prefix in scope, f"{curie} is outside {row['profile']}'s scope"


def test_scoring_is_sane():
    row = {"profile": "pharma", "query": "caffeine", "gold": ["CHEBI:27732"]}
    perfect = score_one(row, [{"curie": "CHEBI:27732"}])
    assert perfect["hit@10"] == 1.0 and perfect["mrr@10"] == 1.0

    missed = score_one(row, [{"curie": "CHEBI:1"}, {"curie": "CHEBI:27732"}])
    assert missed["mrr@10"] == 0.5

    nothing = score_one(row, [])
    assert nothing["hit@10"] == 0.0 and nothing["mrr@10"] == 0.0

    # any-of: one acceptable answer out of two is a hit, not a half.
    two_gold = {"profile": "pharma", "query": "aspirin", "gold": ["CHEBI:15365", "NCIT:C287"]}
    assert score_one(two_gold, [{"curie": "NCIT:C287"}])["hit@10"] == 1.0


# --- the live evaluation ---------------------------------------------------


@pytest.mark.integration
async def test_capture_baseline_if_absent():
    """Write the pre-change baseline the C and D gates measure against.

    Idempotent: once the baseline exists it is never silently overwritten, because
    a baseline that moves with the code is not a baseline.
    """
    if BASELINE.exists():
        pytest.skip(f"baseline already captured at {BASELINE}")
    report = await run_eval()
    BASELINE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    assert len(report) == 30


@pytest.mark.integration
async def test_no_query_regresses_against_the_baseline():
    """THE GATE. Not a threshold — a comparison against what this code used to do."""
    if not BASELINE.exists():
        pytest.skip("no baseline captured yet; run test_capture_baseline_if_absent first")

    baseline = json.loads(BASELINE.read_text())
    current = await run_eval()

    regressions = []
    for key, before in baseline.items():
        after = current.get(key)
        if after is None:
            regressions.append(f"{key}: query disappeared from the fixture")
            continue
        for metric in ("hit@10", "mrr@10"):
            if after[metric] < before[metric]:
                regressions.append(
                    f"{key}: {metric} {before[metric]} -> {after[metric]} "
                    f"(was {before['found'][:3]}, now {after['found'][:3]})"
                )
        for metric in ("out_of_domain@10", "max_ontology_share@10"):
            if after[metric] > before[metric]:
                regressions.append(f"{key}: {metric} {before[metric]} -> {after[metric]}")

    assert not regressions, "Retrieval regressed:\n  " + "\n  ".join(regressions)
