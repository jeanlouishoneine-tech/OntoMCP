# proposer-policy v2026.08

overlaps:
  disease           prefer MONDO; offer DOID, NCIT as alternatives
  human phenotype   prefer HP;  mouse phenotype prefer MP  (the species in the description decides)
  measurable trait  prefer OBA over raw PATO for biomarker / attribute work

notes:
  NCIT   thesaurus, not a formal ontology — weaker axioms; say so when it is the pick (oncology)
  ORDO   CURIEs are "ORDO:NNN". The IRI stem is ".../Orphanet_NNN", so anything that derives a
         prefix from the IRI will mis-spell it — use the CURIE OLS returns, not the IRI.
  HP     OLS mints "HP:NNNNNNN"; this tool's registry key is "HPO". Either spelling resolves.

domain pointers (only where we know we serve a niche the model under-suggests):
  plant / agronomy  consider PECO, PPO, AGRO alongside PO/TO
                    # failing example: "drought stress treatment" returns nothing from PO/TO alone
  fermentation      consider OMP, FOODON, ENVO alongside NCBITaxon/CHEBI/GO
                    # failing example: "bioreactor" is in ENVO, not in CHEBI/GO/NCBITaxon
