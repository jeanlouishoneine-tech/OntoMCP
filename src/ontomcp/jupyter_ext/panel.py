"""ipywidgets search panel for OntoMCP.

A text box, one checkbox per ontology (from the core registry — never hardcoded),
a Search button, and an output area. Each result renders as a term card with
"Copy CURIE" and "Show graph" buttons. All callbacks swallow exceptions and render
them into the output area so nothing raises into the kernel.

ipywidgets is an optional dependency (the ``jupyter`` extra), imported lazily.
"""

import html
import json

from ontomcp.core.config import ONTOLOGIES
from ontomcp.jupyter_ext.client import OntoMCPClient


def _require_ipywidgets():
    try:
        import ipywidgets  # noqa: F401
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(
            "ipywidgets is required for the search panel. "
            "Install it with: pip install 'ontomcp[jupyter]'"
        ) from exc
    return ipywidgets


def _term_card_html(term) -> str:
    """Build the inner HTML for a term card, escaping all OLS-supplied text.

    Pure (no ipywidgets) so the escaping is unit-testable without the jupyter
    extra. OLS labels/definitions are untrusted; ``html.escape`` prevents a value
    containing markup from injecting into the notebook's HTML widget.
    """
    curie = term.get("curie", "?")
    label = term.get("label") or curie
    definition = term.get("definition") or "(no definition)"
    obsolete = bool(term.get("is_obsolete"))

    header = f"<b>{html.escape(label)}</b> &nbsp; <code>{html.escape(curie)}</code>"
    if obsolete:
        header += ' &nbsp; <span style="color:#c0392b">⚠ obsolete</span>'
    return f"{header}<br><span style='color:#555'>{html.escape(definition)}</span>"


def term_card(term, client=None):
    """Build a widget summarising a term dict (label, CURIE, definition, buttons).

    Reused by the graph renderer's node-click handler. ``client`` enables the
    "Show graph" button; without it the button is omitted.
    """
    widgets = _require_ipywidgets()

    curie = term.get("curie", "?")
    body = widgets.HTML(_term_card_html(term))

    copy_btn = widgets.Button(description="Copy CURIE", layout=widgets.Layout(width="110px"))
    out = widgets.Output()

    def on_copy(_):
        out.clear_output()
        with out:
            try:
                from IPython.display import Javascript, display

                # json.dumps yields a correctly-escaped JS string literal (JSON is
                # a JS subset), unlike Python repr which isn't safe JS escaping.
                display(Javascript(f"navigator.clipboard.writeText({json.dumps(curie)});"))
                print(f"Copied {curie}")
            except Exception:
                print(curie)  # fallback: show it for manual copy

    copy_btn.on_click(on_copy)
    buttons = [copy_btn]

    if client is not None:
        graph_btn = widgets.Button(description="Show graph", layout=widgets.Layout(width="110px"))

        def on_graph(_):
            out.clear_output()
            with out:
                try:
                    from IPython.display import display

                    from ontomcp.jupyter_ext.graph import render_graph

                    data = client.graph(curie)
                    if isinstance(data, dict) and data.get("error"):
                        print(f"Could not build graph: {data.get('error')}")
                    else:
                        display(render_graph(data, client))
                except Exception as exc:
                    print(f"Error building graph: {exc}")

        graph_btn.on_click(on_graph)
        buttons.append(graph_btn)

    return widgets.VBox(
        [body, widgets.HBox(buttons), out],
        layout=widgets.Layout(border="1px solid #ddd", padding="6px", margin="4px 0"),
    )


def search_panel(client=None):
    """Build and return the interactive search panel as a VBox widget.

    Args:
        client: Optional ``OntoMCPClient``. Defaults to one pointing at the local API.
    """
    widgets = _require_ipywidgets()
    if client is None:
        client = OntoMCPClient()

    query = widgets.Text(placeholder="e.g. cell death", description="Search:")
    checks = {
        ont: widgets.Checkbox(value=False, description=ont, indent=False) for ont in ONTOLOGIES
    }
    search_btn = widgets.Button(description="Search", button_style="primary")
    results = widgets.Output()

    def on_search(_):
        results.clear_output()
        selected = [ont for ont, cb in checks.items() if cb.value] or None
        with results:
            try:
                hits = client.search(query.value, ontologies=selected)
            except Exception as exc:
                print(f"Search failed: {exc}")
                return
            if (
                isinstance(hits, list)
                and hits
                and isinstance(hits[0], dict)
                and hits[0].get("error")
            ):
                print(f"Search error: {hits[0].get('detail', hits[0]['error'])}")
                return
            if not hits:
                print("No results.")
                return
            from IPython.display import display

            for hit in hits:
                display(term_card(hit, client))

    search_btn.on_click(on_search)

    checkbox_row = widgets.HBox(list(checks.values()), layout=widgets.Layout(flex_flow="row wrap"))
    return widgets.VBox([query, checkbox_row, search_btn, results])
