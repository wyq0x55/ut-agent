"""Offline Golden-to-generation gap classification.

This module is intentionally in ``learning``: reading a reviewed TestCsv is
allowed for characterization and regression, but never for normal generation
or oracle construction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


BASELINE_GAP = "BASELINE_GAP"
PROJECT_RULE_GAP = "PROJECT_RULE_GAP"
FUNCTION_IR_GAP = "FUNCTION_IR_GAP"
OBLIGATION_GAP = "OBLIGATION_GAP"
SOLVER_GAP = "SOLVER_GAP"
EVALUATOR_GAP = "EVALUATOR_GAP"
ORACLE_GAP = "ORACLE_GAP"
SUITE_GAP = "SUITE_GAP"
HARNESS_GAP = "HARNESS_GAP"
PROJECTION_GAP = "PROJECTION_GAP"
GOLDEN_ERROR = "GOLDEN_ERROR"


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
    """Compare semantic cases without inferring a cause from count direction."""
    try:
        from ut_agent.reporting.cases import (
            normalize_golden_cases, match_semantic_cases,
        )
        from .golden import normalize_golden_csv
        actual_normalized = normalize_golden_csv(Path(actual))
        golden_normalized = normalize_golden_csv(Path(golden))
        matching = match_semantic_cases(
            normalize_golden_cases(actual_normalized, source_path=Path(actual)),
            normalize_golden_cases(golden_normalized, source_path=Path(golden)),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        gap = BaselineGap(
            PROJECTION_GAP if Path(actual).is_file() else GOLDEN_ERROR,
            baseline_ref, function, f"无法读取或解析语义 CSV: {exc}",
        )
        return {"equal": False, "gap": gap.to_dict()}
    counts = matching.get("counts", {})
    if not any(counts.get(name) for name in (
            "PARTIAL_MATCH", "MISSING_GENERATED", "EXTRA_GENERATED",
            "AMBIGUOUS_MATCH")):
        return {"equal": True, "gap": None}
    category = (
        ORACLE_GAP if counts.get("PARTIAL_MATCH") else
        SUITE_GAP if counts.get("MISSING_GENERATED") else
        GOLDEN_ERROR if counts.get("EXTRA_GENERATED") else
        SUITE_GAP
    )
    gap = BaselineGap(
        category, baseline_ref, function,
        "semantic testcase matching differs: "
        + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)),
    )
    return {"equal": False, "gap": gap.to_dict()}


def compare_project_with_gaps(project, *, baseline_ref: str) -> list[dict[str, Any]]:
    """Compare an explicitly supplied project corpus and return gap records."""
    results: list[dict[str, Any]] = []
    for unit in project.units:
        if unit.expected is None:
            continue
        golden = unit.expected
        result = compare_semantic_csv(
            unit.testcsv, golden, baseline_ref=baseline_ref, function=unit.name,
        )
        results.append({"function": unit.name, **result})
    return results


__all__ = [
    "BASELINE_GAP", "PROJECT_RULE_GAP", "FUNCTION_IR_GAP", "OBLIGATION_GAP",
    "SOLVER_GAP", "EVALUATOR_GAP", "ORACLE_GAP", "SUITE_GAP",
    "HARNESS_GAP", "PROJECTION_GAP", "GOLDEN_ERROR", "BaselineGap",
    "compare_project_with_gaps", "compare_semantic_csv",
]
