"""Unit tests for the Jupyter panel's untrusted-input handling.

Only the pure helpers are tested here — building the actual widgets needs the
optional ``jupyter`` extra (ipywidgets), so those paths are exercised manually.
The security-critical escaping lives in ``_term_card_html`` precisely so it can
be tested without that dependency.
"""

import json

from ontomcp.jupyter_ext.panel import _term_card_html


def test_term_card_html_escapes_malicious_label():
    html = _term_card_html(
        {"curie": "GO:1", "label": "<img src=x onerror=alert(1)>", "definition": "ok"}
    )
    # The raw tag must not appear; its escaped form must.
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_term_card_html_escapes_malicious_definition():
    html = _term_card_html(
        {"curie": "GO:1", "label": "cell death", "definition": "</span><script>steal()</script>"}
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_term_card_html_escapes_curie():
    html = _term_card_html({"curie": "GO:<b>1</b>", "label": "x", "definition": "y"})
    assert "<b>1</b>" not in html
    assert "GO:&lt;b&gt;1&lt;/b&gt;" in html


def test_term_card_html_keeps_static_markup():
    # Our own wrapper tags are intentionally present; only term text is escaped.
    html = _term_card_html({"curie": "GO:1", "label": "cell death", "definition": "a process"})
    assert "<b>cell death</b>" in html
    assert "<code>GO:1</code>" in html


def test_term_card_html_obsolete_badge():
    html = _term_card_html({"curie": "GO:1", "label": "x", "definition": "y", "is_obsolete": True})
    assert "obsolete" in html


def test_clipboard_js_is_json_escaped():
    # The clipboard call embeds the curie via json.dumps — a curie containing a
    # quote/JS breakout must stay inside the string literal, not escape it.
    hostile = 'GO:1"); alert(1); //'
    literal = json.dumps(hostile)
    js = f"navigator.clipboard.writeText({literal});"

    # The hostile quote is backslash-escaped, so it can't terminate the string
    # literal. Concretely: the breakout requires an UNescaped `");` — every `"`
    # inside the payload is preceded by a backslash.
    assert '\\"); alert(1)' in js  # the quote is escaped -> contained, harmless
    assert '"GO:1\\"' in js  # opening quote, then escaped inner quote
    # And json.dumps round-trips back to the exact original string.
    assert json.loads(literal) == hostile
