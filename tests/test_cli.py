"""CLI degradation tests: every subcommand must fail clean, never with a traceback."""

import json

import pytest

from ontomcp import cli
from ontomcp.core import registry


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.ontomcp/profiles."""
    monkeypatch.setenv("ONTOMCP_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("ONTOMCP_PROFILE", raising=False)
    monkeypatch.delenv("ONTOMCP_ONTOLOGIES", raising=False)
    return tmp_path


def _write_profile(tmp_path, name="t"):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "name": name,
                "ontologies": ["GO"],
                "_derived": {"members": {"GO": {"version": "2026-01-01"}}},
            }
        )
    )
    return path


def _missing_index(*_args, **_kwargs):
    raise registry.IndexError_(
        "Ontology index not found at /nowhere. Run: python scripts/refresh_index.py"
    )


def test_check_reports_a_missing_index_cleanly(tmp_path, monkeypatch, capsys):
    _write_profile(tmp_path)
    monkeypatch.setattr(registry, "load_index", _missing_index)

    code = cli.main(["profile", "check", "t"])

    assert code == 1
    assert "refresh_index" in capsys.readouterr().err


async def _none_version(self, ontology):
    return None


def test_check_reports_unversioned_not_unreachable_when_pinned_as_such(
    tmp_path, monkeypatch, capsys
):
    """A member frozen with `unversioned: true` must be reported as up to date,
    not as unreachable — fetch_ontology_version returns None for both cases, so
    `check` must consult the pinned flag rather than re-deriving the wrong one.
    """
    from ontomcp.core.ols_client import OLSClient

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "t.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "name": "t",
                "ontologies": ["GO"],
                "_derived": {"members": {"GO": {"version": None, "unversioned": True}}},
            }
        )
    )
    monkeypatch.setattr(OLSClient, "fetch_ontology_version", _none_version)

    code = cli.main(["profile", "check", "t"])

    assert code == 0
    out = capsys.readouterr().out
    assert "unversioned" in out
    assert "could not reach OLS" not in out


def test_check_still_reports_unreachable_when_not_pinned_as_unversioned(
    tmp_path, monkeypatch, capsys
):
    """The fix above must not blanket-suppress the unreachable message — a
    member never observed as unversioned and now returning None is still
    reported as unreachable."""
    from ontomcp.core.ols_client import OLSClient

    _write_profile(tmp_path)  # GO, version "2026-01-01", no `unversioned` flag
    monkeypatch.setattr(OLSClient, "fetch_ontology_version", _none_version)

    code = cli.main(["profile", "check", "t"])

    assert code == 0
    out = capsys.readouterr().out
    assert "could not reach OLS" in out


async def _fake_probe_get(self, path, params=None):
    import httpx

    request = httpx.Request("GET", "https://example.org" + path)
    if path.endswith("/terms"):
        return httpx.Response(
            200,
            json={"_embedded": {"terms": [{"obo_id": "GO:1", "iri": "http://x/GO_1"}]}},
            request=request,
        )
    return httpx.Response(200, json={"version": "2026-01-01"}, request=request)


def test_edit_can_shadow_a_starter_profile_name(monkeypatch, capsys):
    """`ontomcp profile edit <starter>` must not refuse just because no user
    profile file exists yet under that name — it's a legitimate shadow-and-save,
    same as the MCP `save_profile` tool already allows (profile.load() falls
    back to STARTERS_DIR).
    """
    from ontomcp.core import profile as profile_mod
    from ontomcp.core.ols_client import OLSClient

    assert "fermentation" in profile_mod.starters()
    assert not profile_mod.profile_path("fermentation").exists()

    monkeypatch.setattr(OLSClient, "_get", _fake_probe_get)

    code = cli.main(["profile", "edit", "fermentation", "--ontologies", "GO,CHEBI"])

    assert code == 0
    err = capsys.readouterr().err
    assert "not found" not in err.lower()


def test_refresh_reports_a_missing_index_cleanly(tmp_path, monkeypatch, capsys):
    _write_profile(tmp_path)
    monkeypatch.setattr(registry, "load_index", _missing_index)

    code = cli.main(["profile", "refresh", "t", "--yes"])

    assert code == 1
    assert "refresh_index" in capsys.readouterr().err


def test_create_reports_a_missing_index_cleanly(monkeypatch, capsys):
    # create -> profile_mod.save -> freeze -> registry.load_index()
    monkeypatch.setattr(registry, "load_index", _missing_index)

    code = cli.main(["profile", "create", "t", "--ontologies", "GO"])

    assert code == 1
    assert "refresh_index" in capsys.readouterr().err
