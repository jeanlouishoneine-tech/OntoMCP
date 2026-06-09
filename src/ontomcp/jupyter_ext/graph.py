"""ipycytoscape renderer for OntoMCP term graphs.

Consumes the ``/graph/{curie}`` payload (``{nodes, edges, focus_curie}``) directly.
Nodes are coloured by their ``role`` (focus/ancestor/descendant/sibling). Clicking
a node shows its term card; double-clicking re-centres the graph on that node.

ipycytoscape is an optional dependency (the ``jupyter`` extra) and is imported
lazily so that importing this module never fails without it.
"""

# role -> node colour (plan.md: teal focus, gray ancestor, purple descendant, coral sibling)
ROLE_COLORS = {
    "focus": "#1abc9c",
    "ancestor": "#95a5a6",
    "descendant": "#9b59b6",
    "sibling": "#e74c3c",
}
_DEFAULT_COLOR = "#7f8c8d"


def _require_ipycytoscape():
    try:
        import ipycytoscape  # noqa: F401
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(
            "ipycytoscape is required for graph rendering. "
            "Install it with: pip install 'ontomcp[jupyter]'"
        ) from exc
    return ipycytoscape


def _to_elements(graph_data):
    """Convert a ``/graph`` payload into ipycytoscape element dicts."""
    nodes = [
        {
            "data": {
                "id": n["curie"],
                "label": n.get("label") or n["curie"],
                "ontology": n.get("ontology"),
                "role": n.get("role", "descendant"),
            }
        }
        for n in graph_data.get("nodes", [])
    ]
    edges = [
        {"data": {"source": e["source"], "target": e["target"], "rel_type": e.get("rel_type", "")}}
        for e in graph_data.get("edges", [])
    ]
    return {"nodes": nodes, "edges": edges}


def _stylesheet():
    style = [
        {
            "selector": "node",
            "style": {
                "label": "data(label)",
                "font-size": "9px",
                "color": "#2c3e50",
                "text-wrap": "wrap",
                "text-max-width": "90px",
                "background-color": _DEFAULT_COLOR,
            },
        },
        {
            "selector": "edge",
            "style": {
                "label": "data(rel_type)",
                "font-size": "7px",
                "curve-style": "bezier",
                "target-arrow-shape": "triangle",
                "line-color": "#bdc3c7",
                "target-arrow-color": "#bdc3c7",
            },
        },
    ]
    for role, color in ROLE_COLORS.items():
        style.append({"selector": f'node[role = "{role}"]', "style": {"background-color": color}})
    return style


def render_graph(graph_data, client=None):
    """Render a term graph as an interactive ipycytoscape widget.

    Args:
        graph_data: A ``/graph`` payload — ``{nodes, edges, focus_curie}``.
        client: Optional ``OntoMCPClient``. When given, clicking a node displays its
            term card and double-clicking re-centres the graph on that node.

    Returns:
        ipywidgets.VBox containing the graph widget and an output area for cards.
    """
    ipycytoscape = _require_ipycytoscape()
    import ipywidgets as widgets

    graph = ipycytoscape.CytoscapeWidget()
    graph.graph.add_graph_from_json(_to_elements(graph_data), directed=True)
    graph.set_style(_stylesheet())

    out = widgets.Output()

    if client is not None:
        _wire_interactions(graph, client, out)

    return widgets.VBox([graph, out])


def _wire_interactions(cyto_widget, client, out):
    """Attach click handler: shows a term card with a Re-centre button.

    ipycytoscape has a bug where registering two handlers for the same widget_type
    (both "click" and "dblclick" on "node") corrupts the internal _interaction_handlers
    trait. We work around this by using only one event type and putting re-centring
    on a button inside the term card instead.
    """
    # Imported here to avoid a circular import (panel imports render_graph).
    from ontomcp.jupyter_ext.panel import term_card

    def on_click(node):
        curie = node["data"]["id"]
        out.clear_output()
        with out:
            try:
                term = client.get_term(curie)
                if isinstance(term, dict) and term.get("error"):
                    print(f"Could not load {curie}: {term.get('detail', term['error'])}")
                else:
                    import ipywidgets as widgets
                    from IPython.display import display

                    recentre_btn = widgets.Button(
                        description="Re-centre graph",
                        layout=widgets.Layout(width="130px"),
                    )

                    def on_recentre(_):
                        try:
                            new_data = client.graph(curie)
                            if isinstance(new_data, dict) and new_data.get("error"):
                                out.clear_output()
                                with out:
                                    print(
                                        f"Could not re-centre on {curie}: {new_data.get('error')}"
                                    )
                                return
                            cyto_widget.graph.clear()
                            cyto_widget.graph.add_graph_from_json(
                                _to_elements(new_data), directed=True
                            )
                            # Replace the now-stale card with a short confirmation so the
                            # output area isn't left blank after the graph re-centres.
                            out.clear_output()
                            with out:
                                print(f"Re-centred on {curie}. Click a node for its details.")
                        except Exception as exc:
                            out.clear_output()
                            with out:
                                print(f"Error re-centring on {curie}: {exc}")

                    recentre_btn.on_click(on_recentre)
                    card = term_card(term, client)
                    display(widgets.VBox([recentre_btn, card]))
            except Exception as exc:  # surface, never raise into the kernel
                print(f"Error loading {curie}: {exc}")

    cyto_widget.on("node", "click", on_click)
