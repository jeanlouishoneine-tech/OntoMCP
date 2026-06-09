"""The ontology tool functions. Both FastMCP and FastAPI call these directly."""

from ontomcp.core.tools.bulk import bulk_annotate
from ontomcp.core.tools.graph import get_term_graph
from ontomcp.core.tools.hierarchy import (
    get_ancestors,
    get_children,
    get_descendants,
    get_parents,
)
from ontomcp.core.tools.mapping import map_across_ontologies
from ontomcp.core.tools.search import search_terms
from ontomcp.core.tools.suggest import suggest_ontology
from ontomcp.core.tools.term import find_synonyms, get_term, validate_term

__all__ = [
    "search_terms",
    "get_term",
    "find_synonyms",
    "validate_term",
    "get_parents",
    "get_children",
    "get_ancestors",
    "get_descendants",
    "suggest_ontology",
    "map_across_ontologies",
    "bulk_annotate",
    "get_term_graph",
]
