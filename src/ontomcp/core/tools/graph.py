"""get_term_graph: a small focus-centred subgraph for visualization.

Composes get_term with the DIRECT-edge hierarchy tools (get_parents,
get_children), then trims to the node cap. Output feeds SVG rendering in chat and
ipycytoscape in Jupyter. Direct edges are used (not the transitive ancestors/
descendants) so the rendered topology is true: every edge is a real one-hop
relationship, never a fabricated direct link to a distant ancestor.
"""

from pathlib import Path

from ontomcp.core.config import DB_PATH, GRAPH_NODE_CAP
from ontomcp.core.ols_client import OLSClient
from ontomcp.core.tools._common import is_error, ols_client
from ontomcp.core.tools.hierarchy import get_children, get_parents
from ontomcp.core.tools.term import get_term


def _node(curie: str, label, ontology, role: str) -> dict:
    return {"curie": curie, "label": label, "ontology": ontology, "role": role}


async def get_term_graph(
    curie: str,
    include_siblings: bool = True,
    *,
    db_path: Path = DB_PATH,
    client: OLSClient | None = None,
) -> tuple[dict, bool]:
    """Build ``({nodes, edges, focus_curie}, cache_hit)`` centred on a term.

    ``role`` is one of focus/ancestor/descendant/sibling. The graph is built from
    DIRECT parents and children (true one-hop ``is_a`` edges), so ``ancestor`` here
    means "direct parent" and ``descendant`` means "direct child" — the edges are
    real, never a fabricated link to a distant transitive ancestor. Hard-capped at
    GRAPH_NODE_CAP nodes: descendants are trimmed first, then siblings, then
    ancestors, always keeping the focus. Edges to trimmed nodes are dropped.
    ``cache_hit`` is True only when every underlying lookup was a cache hit
    (hierarchy tools always fetch live, so in practice this is False).

    Note: siblings come from the focus's first direct parent via get_children,
    which is itself capped, so for a parent with very many children the sibling set
    is a bounded (curie-sorted, hence stable) subset rather than the full list.
    """
    async with ols_client(client) as cli:
        focus, focus_hit = await get_term(curie, db_path=db_path, client=cli)
        if is_error(focus):
            return focus, focus_hit
        focus_curie = focus["curie"]

        parents, anc_hit = await get_parents(focus_curie, db_path=db_path, client=cli)
        children, desc_hit = await get_children(focus_curie, db_path=db_path, client=cli)
        parents = [] if is_error(parents) else parents
        children = [] if is_error(children) else children

        sib_hit = True
        siblings: list[dict] = []
        if include_siblings and parents:
            parent = parents[0]["curie"]
            sibs, sib_hit = await get_children(parent, db_path=db_path, client=cli)
            if not is_error(sibs):
                # get_children is capped and ordered by OLS; sort by curie so the
                # sibling subset is stable across runs when the parent is large.
                siblings = sorted(
                    (s for s in sibs if s["curie"] != focus_curie),
                    key=lambda s: s["curie"],
                )

    cache_hit = focus_hit and anc_hit and desc_hit and sib_hit

    # Build node set with role precedence: focus wins, then ancestor/descendant/sibling.
    nodes: dict[str, dict] = {
        focus_curie: _node(focus_curie, focus.get("label"), focus.get("ontology"), "focus")
    }

    def add(items: list[dict], role: str) -> None:
        for it in items:
            c = it["curie"]
            if c not in nodes:
                ont = c.split(":", 1)[0]
                nodes[c] = _node(c, it.get("label"), ont, role)

    add(parents, "ancestor")
    add(children, "descendant")
    add(siblings, "sibling")

    # Trim to cap, removing lowest-priority roles first, never the focus.
    if len(nodes) > GRAPH_NODE_CAP:
        priority = {"focus": 0, "ancestor": 1, "sibling": 2, "descendant": 3}
        ordered = sorted(nodes.values(), key=lambda n: priority[n["role"]])
        kept = ordered[:GRAPH_NODE_CAP]
        nodes = {n["curie"]: n for n in kept}

    edges: list[dict] = []

    def add_edge(source: str, target: str) -> None:
        if source in nodes and target in nodes:
            edges.append({"source": source, "target": target, "rel_type": "is_a"})

    for a in parents:
        add_edge(focus_curie, a["curie"])  # focus is_a direct parent
    for d in children:
        add_edge(d["curie"], focus_curie)  # direct child is_a focus
    if include_siblings and parents:
        parent = parents[0]["curie"]
        for s in siblings:
            add_edge(s["curie"], parent)  # sibling is_a parent

    return {"nodes": list(nodes.values()), "edges": edges, "focus_curie": focus_curie}, cache_hit
