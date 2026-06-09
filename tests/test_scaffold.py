"""Phase 1 smoke tests: the scaffold imports and the config registry is sane.

Replaced/augmented by real test modules in Phases 2+.
"""

import ontomcp
from ontomcp.core import config


def test_version():
    assert ontomcp.__version__ == "0.1.0"


def test_registry_includes_core_and_pharma_ontologies():
    # The original free set plus the pharma/oncology additions (NCIT, DOID, PR).
    expected = {"GO", "MONDO", "HPO", "CHEBI", "UBERON", "CL", "EFO", "MESH", "NCIT", "DOID", "PR"}
    assert expected <= set(config.ONTOLOGIES)


def test_iri_templates_cover_every_ontology():
    assert set(config.IRI_TEMPLATES) == set(config.ONTOLOGIES)


def test_ols_base_url():
    assert config.OLS_BASE_URL == "https://www.ebi.ac.uk/ols4/api"


def test_user_agent_tracks_version():
    assert config.USER_AGENT == f"OntoMCP/{ontomcp.__version__}"


def test_caps_are_ordered():
    assert config.BULK_WARN < config.BULK_MAX
    assert config.GRAPH_NODE_CAP <= config.DESCENDANTS_CAP


def test_server_entrypoints_import():
    # The FastAPI app is implemented (Phase 5): its app object imports cleanly.
    from ontomcp.api.main import app

    assert app is not None

    # The MCP server is implemented (Phase 6): its FastMCP app and run entrypoint
    # import cleanly. (We don't call run() — it would start the stdio server.)
    from ontomcp.mcp_server.server import mcp, run

    assert mcp is not None
    assert callable(run)
