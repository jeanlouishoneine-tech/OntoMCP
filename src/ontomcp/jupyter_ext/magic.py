"""IPython cell magic ``%%ontomcp`` for DataFrame annotation.

Usage:

    %%ontomcp annotate --df cells --col cell_type --ontology CL
    (cell body may be empty)

Resolves the named DataFrame from the user namespace, annotates one column via the
``/bulk`` endpoint, and writes back ``<col>_curie``, ``<col>_label``, ``<col>_score``.
The annotated DataFrame is returned so the cell displays it.

Load with: ``%load_ext ontomcp.jupyter_ext.magic``
"""

import shlex

from IPython.core.magic import Magics, cell_magic, magics_class
from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring

from ontomcp.jupyter_ext.client import OntoMCPClient


@magics_class
class OntoMCPMagics(Magics):
    """Cell magics backed by a single OntoMCP client."""

    def __init__(self, shell):
        super().__init__(shell)
        self._client = OntoMCPClient()

    # mypy can't see that @cell_magic("ontomcp") returns a decorator (its stubs
    # type the named form as a no-arg decorator), so it flags the wrapped method.
    @cell_magic("ontomcp")  # type: ignore[arg-type]
    def ontomcp(self, line, cell):
        """Dispatch ``%%ontomcp <subcommand> ...``. v1 supports ``annotate``."""
        parts = shlex.split(line)
        if not parts:
            print("Usage: %%ontomcp annotate --df NAME --col COL [--ontology ONT] [--threshold T]")
            return
        sub, rest = parts[0], " ".join(parts[1:])
        if sub == "annotate":
            return self._annotate(rest)
        print(f"Unknown subcommand '{sub}'. Supported: annotate")

    @magic_arguments()
    @argument("--df", required=True, help="Name of the DataFrame variable")
    @argument("--col", required=True, help="Column to annotate")
    @argument("--ontology", default=None, help="Restrict matches to this ontology, e.g. CL")
    @argument("--threshold", type=float, default=0.8, help="Match score cutoff (0-1)")
    def _annotate(self, argline):
        args = parse_argstring(self._annotate, argline)

        df = self.shell.user_ns.get(args.df)
        if df is None:
            print(f"No variable named '{args.df}' in the notebook.")
            return
        # Duck-type rather than import pandas just to isinstance-check.
        if not hasattr(df, "columns") or not hasattr(df, "__setitem__"):
            print(f"'{args.df}' is not a DataFrame.")
            return
        if args.col not in df.columns:
            print(f"Column '{args.col}' not found. Available: {list(df.columns)}")
            return

        terms = df[args.col].astype(str).tolist()
        try:
            result = self._client.bulk(terms, ontology_hint=args.ontology, threshold=args.threshold)
        except Exception as exc:
            print(f"Annotation failed: {exc}")
            return
        if isinstance(result, dict) and result.get("error"):
            print(f"Annotation error: {result.get('error')} ({result.get('detail', '')})")
            return
        if result.get("warning"):
            print(result["warning"])

        # Map each input string to its best match. Inputs may repeat, so build a
        # lookup keyed on the input value and apply it column-wise.
        best = {}
        for row in result.get("results", []):
            match = row.get("best_match")
            best[row["input"]] = match  # dict or None

        col = args.col
        df[f"{col}_curie"] = [(best.get(t) or {}).get("curie") for t in terms]
        df[f"{col}_label"] = [(best.get(t) or {}).get("label") for t in terms]
        df[f"{col}_score"] = [(best.get(t) or {}).get("score") for t in terms]
        return df


def load_ipython_extension(ipython):
    """Register the magics so ``%load_ext ontomcp.jupyter_ext.magic`` works."""
    ipython.register_magics(OntoMCPMagics)
