"""Jupyter extension package.

Public surface for notebook users. Heavy UI deps (ipywidgets, ipycytoscape) are
imported lazily inside the panel/graph functions, so importing this package is cheap
and never fails when the ``jupyter`` extra isn't installed.
"""

from ontomcp.jupyter_ext.client import OntoMCPClient, OntoMCPConnectionError
from ontomcp.jupyter_ext.graph import render_graph
from ontomcp.jupyter_ext.panel import search_panel, term_card

__all__ = [
    "OntoMCPClient",
    "OntoMCPConnectionError",
    "render_graph",
    "search_panel",
    "term_card",
]
