#!/usr/bin/env python3
"""Regenerate src/ontomcp/data/ols4-index.json from the OLS4 /ontologies collection.

An argparse shell, nothing more. All validation lives in ``core.registry`` because
``pyproject.toml`` packages only ``src/ontomcp`` — nothing under ``scripts/`` is
importable or reachable by pytest, so logic placed here would be untestable.

Usage:
    python scripts/refresh_index.py                       # fetch live, write if clean
    python scripts/refresh_index.py --from-fixture PATH    # offline, from a captured response
    python scripts/refresh_index.py --allow-removals ORDO,XYZ
    python scripts/refresh_index.py --dry-run              # report, write nothing

Index changes land by PR with a CODEOWNERS reviewer. The script refuses to write a
degraded index rather than orphaning every frozen profile; overriding that refusal
is deliberately not a flag.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ontomcp.core.config import INDEX_PATH, OLS_BASE_URL, USER_AGENT  # noqa: E402
from ontomcp.core.registry import validate_index  # noqa: E402


def fetch_live() -> dict:
    """GET /ontologies?size=1000. One page — OLS4 served 286 entries on 2026-08-15."""
    import httpx

    response = httpx.get(
        f"{OLS_BASE_URL}/ontologies",
        params={"size": 1000},
        headers={"User-Agent": USER_AGENT},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--from-fixture", type=Path, help="Read a captured response instead of OLS")
    parser.add_argument("--out", type=Path, default=INDEX_PATH, help="Index path to write")
    parser.add_argument(
        "--allow-removals",
        default="",
        help="Comma-separated prefixes OLS4 genuinely dropped; each must be named exactly",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report and write nothing")
    args = parser.parse_args()

    payload = json.loads(args.from_fixture.read_text()) if args.from_fixture else fetch_live()

    previous = json.loads(args.out.read_text()) if args.out.exists() else None
    removals = tuple(x.strip().upper() for x in args.allow_removals.split(",") if x.strip())

    index, problems = validate_index(payload, previous=previous, allow_removals=removals)

    if problems:
        print(f"REFUSING to write {args.out} — {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    index["generated_at"] = f"{datetime.now(UTC):%Y-%m-%d}"
    print(f"{len(index['ontologies'])} ontologies, index_version {index['index_version']}")
    for note in index["review"]:
        print(f"  REVIEW: {note}")

    if args.dry_run:
        print("--dry-run: nothing written.")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
