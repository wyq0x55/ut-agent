"""Offline Golden-to-generation gap classification.

This module is intentionally in ``learning``: reading a reviewed TestCsv is
allowed for characterization and regression, but never for normal generation
or oracle construction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .golden import semantic_csv_signature

BASELINE_GAP = "BASELINE_GAP"
BASELINE_CONVERSION_MISS = "BASELINE_CONVERSION_MISS"
BASELINE_IMPLICIT_REQUIREMENT = "BASELINE_IMPLICIT_REQUIREMENT"
STABLE_HUMAN_CONVENTION = "STABLE_HUMAN_CONVENTION"
PROJECT_SPECIFIC_ADDITION = "PROJECT_SPECIFIC_ADDITION"
GOLDEN_ERROR = "GOLDEN_ERROR"
GENERATOR_BUG = "GENERATOR_BUG"


@dataclass(frozen=True)
class BaselineGap:
    category: str
    baseline_ref: str
    function: str
    detail: str
    code: str = BASELINE_GAP

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare_semantic_csv(actual: Path, golden: Path, *, baseline_ref: str,
                        function: str = "") -> dict[str, Any]:
    """Compare semantic signatures and retain an explicit review category.

    The comparison does not infer that the reviewed case is redundant.  A
    human or a later baseline-conversion step must classify the gap.
    """
    try:
        actual_signature = semantic_csv_signature(Path(actual))
        golden_signature = semantic_csv_signature(Path(golden))
    except (OSError, UnicodeError, ValueError) as exc:
        gap = BaselineGap(
            GENERATOR_BUG if Path(actual).is_file() else BASELINE_CONVERSION_MISS,
            baseline_ref, function, f"无法读取或解析语义 CSV: {exc}",
        )
        return {"equal": False, "gap": gap.to_dict()}
    if actual_signature == golden_signature:
        return {"equal": True, "gap": None}
    actual_cases = len(actual_signature.get("cases", ()))
    golden_cases = len(golden_signature.get("cases", ()))
    category = (
        BASELINE_IMPLICIT_REQUIREMENT if golden_cases > actual_cases
        else BASELINE_CONVERSION_MISS
    )
    gap = BaselineGap(
        category, baseline_ref, function,
        f"semantic signature differs: generated_cases={actual_cases}, "
        f"golden_cases={golden_cases}",
    )
    return {"equal": False, "gap": gap.to_dict()}


def compare_project_with_gaps(project, *, baseline_ref: str) -> list[dict[str, Any]]:
    """Compare an explicitly supplied project corpus and return gap records."""
    results: list[dict[str, Any]] = []
    for unit in project.units:
        if unit.expected is None:
            continue
        golden = unit.expected.parent / "TestCsv.csv"
        result = compare_semantic_csv(
            unit.testcsv, golden, baseline_ref=baseline_ref, function=unit.name,
        )
        results.append({"function": unit.name, **result})
    return results


__all__ = [
    "BASELINE_CONVERSION_MISS", "BASELINE_GAP", "BASELINE_IMPLICIT_REQUIREMENT",
    "GENERATOR_BUG", "GOLDEN_ERROR", "PROJECT_SPECIFIC_ADDITION",
    "STABLE_HUMAN_CONVENTION", "BaselineGap", "compare_project_with_gaps",
    "compare_semantic_csv",
]
