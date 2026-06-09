"""suggest_ontology: pure keyword logic mapping a research context to ontologies.

No OLS call and no cache — deterministic local reasoning only.
"""

from ontomcp.core.config import ONTOLOGIES
from ontomcp.core.ols_client import _ONTOLOGY_SLUGS

# Ordered (keywords, ontology codes). First match wins ordering; codes are
# collected in first-seen order across all matching rules.
_KEYWORD_RULES: list[tuple[tuple[str, ...], list[str]]] = [
    (("single cell", "cell type", "single-cell"), ["CL", "GO"]),
    (("drug", "compound", "chemical", "molecule"), ["CHEBI", "NCIT", "MESH"]),
    (("target", "receptor", "kinase", "antibody"), ["PR", "CHEBI"]),
    (("cancer", "tumor", "tumour", "oncology", "carcinoma", "neoplasm"), ["NCIT", "MONDO", "DOID"]),
    (("adverse event", "safety", "toxicity"), ["MESH", "NCIT"]),
    (("patient", "phenotype", "clinic"), ["HPO", "MONDO"]),
    (("disease", "disorder", "syndrome"), ["MONDO", "DOID", "HPO"]),
    (("tissue", "organ", "anatomy", "anatomic"), ["UBERON"]),
    (("gwas", "trait", "assay", "experiment"), ["EFO"]),
    (("protein", "proteoform", "complex"), ["PR", "GO"]),
    (("gene", "pathway", "biological process"), ["GO"]),
    (("publication", "mesh", "pubmed", "literature"), ["MESH"]),
]

# A couple of representative terms per ontology, purely illustrative. Every
# registry code has an entry so suggestions always carry examples.
_EXAMPLE_TERMS: dict[str, list[str]] = {
    "GO": ["GO:0008219 (cell death)", "GO:0006915 (apoptotic process)"],
    "MONDO": ["MONDO:0004992 (cancer)", "MONDO:0005148 (type 2 diabetes)"],
    "HPO": ["HP:0001250 (seizure)", "HP:0000252 (microcephaly)"],
    "CHEBI": ["CHEBI:15377 (water)", "CHEBI:27732 (caffeine)"],
    "UBERON": ["UBERON:0002107 (liver)", "UBERON:0000955 (brain)"],
    "CL": ["CL:0000236 (B cell)", "CL:0000084 (T cell)"],
    "EFO": ["EFO:0000400 (diabetes mellitus)", "EFO:0004340 (body mass index)"],
    "MESH": ["MESH:D003643 (death)", "MESH:D009369 (neoplasms)"],
    "NCIT": ["NCIT:C9305 (malignant neoplasm)", "NCIT:C49236 (therapeutic procedure)"],
    "DOID": ["DOID:162 (cancer)", "DOID:9352 (type 2 diabetes mellitus)"],
    "PR": ["PR:000000001 (protein)", "PR:P04637 (cellular tumor antigen p53)"],
}


def _entry(code: str, rationale: str) -> dict:
    slug = _ONTOLOGY_SLUGS.get(code, code.lower())
    return {
        "ontology": code,
        "rationale": rationale,
        "example_terms": _EXAMPLE_TERMS.get(code, []),
        "ols_url": f"https://www.ebi.ac.uk/ols4/ontologies/{slug}",
    }


def suggest_ontology(context: str) -> tuple[list[dict], bool]:
    """Suggest which ontologies fit a free-text research context.

    Returns ``(results, cache_hit)``. ``cache_hit`` is always False (pure local
    reasoning — no cache is involved). ``results`` is
    ``[{ontology, rationale, example_terms, ols_url}, ...]``, falling back to
    GO + MONDO when no keyword matches.
    """
    text = (context or "").lower()
    ordered: list[str] = []
    for keywords, codes in _KEYWORD_RULES:
        if any(kw in text for kw in keywords):
            for code in codes:
                if code not in ordered:
                    ordered.append(code)

    if not ordered:
        rationale = "No domain keyword matched; defaulting to general gene and disease ontologies."
        return [_entry("GO", rationale), _entry("MONDO", rationale)], False

    domain_rationale = "Matched the research context to this ontology's domain: "
    return [_entry(code, domain_rationale + ONTOLOGIES[code]["domain"]) for code in ordered], False
