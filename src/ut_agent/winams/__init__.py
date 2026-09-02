"""Target adapters.

Submodules are intentionally loaded on demand so the rule layer can consume
the projection model without importing the CSV renderer back into the rules.
"""

__all__ = ["csv_render"]
