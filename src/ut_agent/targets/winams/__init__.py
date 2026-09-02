"""WinAMS execution model, harness, and deterministic serialization adapter.

Submodules are intentionally lazy: the toolchain's standalone harness uses
the stub module, while the full project adapter uses the toolchain.  Eagerly
importing both would create a reverse initialization cycle.
"""

__all__ = [
    "csv", "define_var", "harness", "index", "project", "projection", "stub",
    "validation",
]
