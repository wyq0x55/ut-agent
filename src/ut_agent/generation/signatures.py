"""Stable semantic signatures used by the deterministic generation engine."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def branch_family(branch: dict[str, Any]) -> dict[str, Any]:
    """Normalize a branch shape without retaining project-local names/values."""
    atoms = [
        {
            "op": str(atom.get("op", "")),
            "boundary_class": str(atom.get("boundary_class", "unknown")),
            "masked": bool(atom.get("masked", False)),
            "mask_width": atom.get("mask_width"),
        }
        for atom in branch.get("atoms", [])
    ]
    atoms.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return {
        "kind": str(branch.get("kind", "")),
        "connective": str(branch.get("connective", "single")),
        "atom_count": len(atoms),
        "atoms": atoms,
    }
