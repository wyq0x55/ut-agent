"""Offline comparison of generated WinAMS artifacts with reviewed Goldens.

Golden comparison is deliberately kept outside ``targets.winams``.  The
target adapter owns how a semantic suite is serialized; this module owns the
optional, read-only comparison against historical human-reviewed artifacts.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ut_agent.targets.winams.project import GeneratedProject

from .golden import semantic_csv_signature


def compare_testcsv(project: GeneratedProject) -> list[tuple[str, bool]]:
    """Compare generated TestCsv files with explicitly declared Goldens."""
    result: list[tuple[str, bool]] = []
    for unit in project.units:
        if unit.expected is None:
            result.append((unit.name, True))
            continue
        golden = unit.expected.parent / "TestCsv.csv"
        try:
            actual = semantic_csv_signature(unit.testcsv)
            expected = semantic_csv_signature(golden)
            result.append((unit.name, actual == expected))
        except (OSError, UnicodeError, ValueError):
            result.append((unit.name, False))
    return result


__all__ = ["compare_testcsv"]
