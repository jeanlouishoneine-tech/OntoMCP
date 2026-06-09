"""Shared helpers for the tool layer: ontology normalization and client lifecycle.

Kept tiny on purpose. Business logic lives in the individual tool modules.
"""

from contextlib import asynccontextmanager

from ontomcp.core.config import ONTOLOGIES
from ontomcp.core.ols_client import OLSClient, normalize_curie


def safe_normalize_curie(curie: str) -> tuple[str | None, dict | None]:
    """Normalize a CURIE, returning (normalized, None) or (None, error_dict).

    Tools must never raise; an unparseable CURIE becomes a structured error.
    Callers guard with ``if norm is None: return err_or_default(err), False`` so
    the success path has ``norm`` typed as ``str``.
    """
    try:
        return normalize_curie(curie), None
    except (ValueError, AttributeError) as exc:
        return None, {"error": "bad_curie", "detail": str(exc), "curie": curie}


def normalize_ontologies(ontologies: list[str] | None) -> list[str] | None:
    """Uppercase ontology codes and drop any unknown to the registry.

    Returns None when the input is empty or every code is unknown — callers treat
    None as "search every ontology in the registry".
    """
    if not ontologies:
        return None
    valid = [o.upper() for o in ontologies if o.upper() in ONTOLOGIES]
    return valid or None


@asynccontextmanager
async def ols_client(client: OLSClient | None):
    """Yield ``client`` if provided, else a fresh OLSClient closed on exit.

    Lets every tool accept an optional injected client (tests, bulk reuse) while
    owning the lifecycle of a default one.
    """
    if client is not None:
        yield client
        return
    async with OLSClient() as owned:
        yield owned


def is_error(result) -> bool:
    """True if an OLS call returned a structured error (dict, or list of them)."""
    if isinstance(result, dict):
        return "error" in result
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return "error" in result[0]
    return False
