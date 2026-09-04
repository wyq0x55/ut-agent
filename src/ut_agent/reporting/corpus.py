"""Deterministic project-corpus validation and semantic gap reporting.

This module is deliberately downstream of generation.  It reads the explicit
project index, generated FunctionIR/test-intent/CSV artifacts, and reviewed
Golden CSVs; it never supplies Golden data to the generator or oracle.
"""
from __future__ import annotations

import hashlib
import json
import csv
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ut_agent.learning.golden import normalize_golden_csv, semantic_csv_signature
from .cases import (
    AMBIGUOUS_MATCH, EXACT_SEMANTIC_MATCH, EQUIVALENT_REPRESENTATIVE,
    EXTRA_GENERATED, MISSING_GENERATED, PARTIAL_MATCH,
    match_semantic_cases, normalize_generated_cases, normalize_golden_cases,
)


STANDARD_GAP_CATEGORIES = (
    "BASELINE_GAP",
    "PROJECT_RULE_GAP",
    "FUNCTION_IR_GAP",
    "OBLIGATION_GAP",
    "SOLVER_GAP",
    "EVALUATOR_GAP",
    "ORACLE_GAP",
    "SUITE_GAP",
    "HARNESS_GAP",
    "PROJECTION_GAP",
    "GOLDEN_ERROR",
)

_DIMENSION_OWNERS = {
    "testcase_count": "generation/suite",
    "viewpoint": "generation/obligation",
    "condition_combination": "generation/obligation",
    "boundary_domain": "baseline/generation",
    "stub": "targets/winams/harness",
    "oracle": "generation/oracle",
    "required_values": "generation/oracle",
    "projection": "targets/winams/projection",
}

_CATEGORY_OWNERS = {
    "BASELINE_GAP": "baseline",
    "PROJECT_RULE_GAP": "project-context",
    "FUNCTION_IR_GAP": "tooling/ut-clang-extract",
    "OBLIGATION_GAP": "generation/obligation",
    "SOLVER_GAP": "generation/solver",
    "EVALUATOR_GAP": "generation/evaluator",
    "ORACLE_GAP": "generation/oracle",
    "SUITE_GAP": "generation/suite",
    "HARNESS_GAP": "targets/winams/harness",
    "PROJECTION_GAP": "targets/winams/projection",
    "GOLDEN_ERROR": "reviewed-golden",
}


@dataclass(frozen=True)
class ProjectCorpusManifest:
    """Resolved paths and immutable scope for one project validation run."""

    path: Path
    project_id: str
    context_manifest: Path
    scope: str
    index_csv: Path
    product_root: Path
    golden_root: Path
    baseline_document: Path
    baseline_sheet: str
    baseline_revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project": {
                "id": self.project_id,
                "context_manifest": str(self.context_manifest),
                "scope": self.scope,
            },
            "corpus": {
                "index_csv": str(self.index_csv),
                "product_root": str(self.product_root),
                "golden_root": str(self.golden_root),
            },
            "evidence": {
                "baseline_document": str(self.baseline_document),
                "baseline_sheet": self.baseline_sheet,
                "baseline_revision": self.baseline_revision,
            },
        }


def _resolve(base: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()


def load_corpus_manifest(path: Path) -> ProjectCorpusManifest:
    """Load and schema-validate a project corpus manifest."""
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        schema_path = Path(__file__).resolve().parents[3] / "schemas" / "project-corpus.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        from jsonschema import Draft202012Validator
        Draft202012Validator(schema).validate(raw)
    except FileNotFoundError as exc:
        raise ValueError(f"项目语料 manifest 或 schema 不存在: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"项目语料 manifest 无效: {path}") from exc

    base = path.parent
    project = raw["project"]
    corpus = raw["corpus"]
    evidence = raw["evidence"]
    return ProjectCorpusManifest(
        path=path,
        project_id=project["id"],
        context_manifest=_resolve(base, project["context_manifest"]),
        scope=project["scope"],
        index_csv=_resolve(base, corpus["index_csv"]),
        product_root=_resolve(base, corpus["product_root"]),
        golden_root=_resolve(base, corpus["golden_root"]),
        baseline_document=_resolve(base, evidence["baseline_document"]),
        baseline_sheet=evidence["baseline_sheet"],
        baseline_revision=evidence["baseline_revision"],
    )


def validate_corpus_paths(manifest: ProjectCorpusManifest) -> None:
    """Fail before extraction when the declared evidence boundary is absent."""
    files = {
        "context_manifest": manifest.context_manifest,
        "index_csv": manifest.index_csv,
        "baseline_document": manifest.baseline_document,
    }
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"项目语料 {name} 不存在: {path}")
    for name, path in {
        "product_root": manifest.product_root,
        "golden_root": manifest.golden_root,
    }.items():
        if not path.is_dir():
            raise FileNotFoundError(f"项目语料 {name} 不存在: {path}")


def _index_tail(value: str, marker: tuple[str, ...]) -> Path | None:
    parts = [item for item in re.split(r"[\\/]", value.strip()) if item]
    lowered = [item.lower() for item in parts]
    wanted = [item.lower() for item in marker]
    for index in range(len(parts) - len(wanted) + 1):
        if lowered[index:index + len(wanted)] == wanted:
            tail = parts[index + len(wanted):]
            return Path(*tail) if tail else None
    return None


def preflight_corpus(manifest: ProjectCorpusManifest) -> tuple[dict[str, Any], ...]:
    """Find indexed fixture gaps before the C++ extractor is invoked."""
    result: list[dict[str, Any]] = []
    if not manifest.index_csv.is_file():
        return ({
            "row": None, "function": None, "source_path": None,
            "target_rel": None, "status": "BLOCKED",
            "reason": "FIXTURE_MISSING", "detail": "index CSV 不存在",
        },)
    try:
        rows = list(csv.reader(
            manifest.index_csv.read_text(encoding="cp932").splitlines()
        ))
    except (OSError, UnicodeError) as exc:
        return ({
            "row": None, "function": None, "source_path": str(manifest.index_csv),
            "target_rel": None, "status": "BLOCKED",
            "reason": "FIXTURE_MISSING", "detail": str(exc),
        },)
    for row_number, values in enumerate(rows, 1):
        if not values or all(not value.strip() for value in values):
            continue
        function = values[2].strip() if len(values) > 2 else None
        if len(values) < 5:
            result.append({
                "row": row_number, "function": function, "source_path": None,
                "target_rel": None, "status": "BLOCKED",
                "reason": "FIXTURE_MISSING", "detail": "index 行少于 5 列",
            })
            continue
        source_rel = _index_tail(values[3], ("Product", "src"))
        if source_rel is not None:
            source_path = (manifest.product_root / "src" / source_rel).resolve()
        else:
            source_rel = _index_tail(values[3], ("Soft",))
            source_path = ((manifest.product_root / source_rel).resolve()
                           if source_rel is not None else None)
        if source_path is None or not source_path.is_file():
            result.append({
                "row": row_number, "function": function,
                "source_path": str(source_path) if source_path else values[3],
                "target_rel": None, "status": "BLOCKED",
                "reason": "FIXTURE_MISSING", "detail": "indexed source 不存在",
            })
    return tuple(result)


def golden_for_unit(manifest: ProjectCorpusManifest, unit: Any) -> Path | None:
    """Resolve one reviewed Golden from the index target mapping.

    The project index owns the target directory mapping.  A single filename
    suffix such as ``...ramdf1.csv`` is accepted only when it is unambiguous;
    no testcase rows or values are inferred from the Golden.
    """
    target_dir = manifest.golden_root / Path(unit.row.target_rel) / "TestCsv"
    exact = target_dir / f"{unit.row.function}.csv"
    if exact.is_file():
        return exact
    if not target_dir.is_dir():
        return None
    prefix = unit.row.function.lower()
    candidates = sorted(
        (item for item in target_dir.iterdir()
         if item.is_file() and item.suffix.lower() == ".csv"
         and item.stem.lower().startswith(prefix)),
        key=lambda item: item.name.lower(),
    )
    return candidates[0] if len(candidates) == 1 else None


def _counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是 object: {path}")
    return value


def normalize_generated_manifest(path: Path) -> dict[str, Any]:
    """Extract semantic generation metrics from one test-intents document."""
    raw = _read_json(path)
    intents = raw.get("intents", [])
    if not isinstance(intents, list):
        raise ValueError(f"intents 必须是 array: {path}")
    obligations = [
        item.get("obligation", {}) for item in intents
        if isinstance(item, dict) and isinstance(item.get("obligation", {}), dict)
    ]
    validations = [
        item.get("validation", {}) for item in intents
        if isinstance(item, dict) and isinstance(item.get("validation", {}), dict)
    ]
    solves = raw.get("solve_results", [])
    evaluations = raw.get("evaluations", [])
    if not isinstance(solves, list):
        solves = []
    if not isinstance(evaluations, list):
        evaluations = []
    input_keys = sorted({
        str(key) for item in intents if isinstance(item, dict)
        for key in (item.get("inputs", {}) or {})
    })
    expected_keys = sorted({
        str(key) for item in intents if isinstance(item, dict)
        for key in (item.get("expected", {}) or {})
    })
    stub_keys = sorted({
        str(key) for item in intents if isinstance(item, dict)
        for key in (item.get("stub_behavior", {}) or {})
    })
    return {
        "status": str(raw.get("status", "UNKNOWN")),
        "csv_written": bool(raw.get("csv_written", False)),
        "csv_kind": str(raw.get("csv_kind", "not_written")),
        "csv_intent_count": (
            raw.get("csv_intent_count")
            if isinstance(raw.get("csv_intent_count"), int) else None
        ),
        "intent_count": len(intents),
        "validated_intent_count": sum(
            item.get("status") == "VALIDATED" and not item.get("errors")
            for item in validations
        ),
        "obligation_kinds": _counts([
            str(item.get("kind", "unknown")) for item in obligations
        ]),
        "outcomes": _counts([
            "TRUE" if item.get("outcome") is True else
            "FALSE" if item.get("outcome") is False else "UNSPECIFIED"
            for item in obligations
        ]),
        "boundary_classes": _counts([
            str(item.get("boundary_class")) for item in obligations
            if item.get("boundary_class") is not None
        ]),
        "pair_count": len({
            item.get("pair_id") for item in obligations
            if item.get("pair_id")
        }),
        "solve_statuses": _counts([
            str(item.get("status", "unknown")) for item in solves
            if isinstance(item, dict)
        ]),
        "evaluation_count": len(evaluations),
        "evaluation_complete_count": sum(
            bool(item.get("complete")) for item in evaluations
            if isinstance(item, dict)
        ),
        "input_keys": input_keys,
        "expected_keys": expected_keys,
        "stub_keys": stub_keys,
        "issues": sorted(str(item) for item in (raw.get("issues", []) or [])),
        "obligation_count": len(obligations),
        "evaluation_statuses": _counts([
            str(item.get("status", "unknown")) for item in evaluations
            if isinstance(item, dict)
        ]),
        "validation_statuses": _counts([
            str(item.get("status", "unknown")) for item in validations
            if isinstance(item, dict)
        ]),
    }


def _generated_viewpoints(manifest: dict[str, Any]) -> dict[str, Any]:
    kinds = manifest.get("obligation_kinds", {})
    return {"counts": dict(kinds), "labels": []}


def _generated_boundary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_value_classes": {},
        "expected_value_classes": {},
        "all_value_classes": dict(manifest.get("boundary_classes", {})),
    }


def _generated_stub(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "declaration_count": 0,
        "declarations": [],
        "columns": list(manifest.get("stub_keys", [])),
    }


def _generated_oracle(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "output_count": len(manifest.get("expected_keys", [])),
        "columns": list(manifest.get("expected_keys", [])),
    }


def _generated_projection(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_count": len(manifest.get("input_keys", [])),
        "output_count": len(manifest.get("expected_keys", [])),
        "comment_columns": [],
        "observed_label_count": 0,
        "scenario_count": manifest.get("intent_count", 0),
    }


def _generated_required_values(manifest: dict[str, Any]) -> dict[str, Any]:
    return {"inputs": [], "expected": []}


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {} or value == 0


def _dimension(generated: Any, golden: Any) -> dict[str, Any]:
    if generated == golden:
        status = "equal"
    elif isinstance(generated, (int, float)) and isinstance(golden, (int, float)):
        status = "missing_generated" if generated < golden else "extra_generated"
    elif _empty(generated) and not _empty(golden):
        status = "missing_generated"
    elif _empty(golden) and not _empty(generated):
        status = "extra_generated"
    else:
        status = "different"
    return {"status": status, "generated": generated, "golden": golden}


def _comparison_dimensions(
    generated_manifest: dict[str, Any],
    generated_csv: dict[str, Any] | None,
    golden: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    generated_csv = generated_csv or {}
    generated_count = generated_csv.get(
        "testcase_count", generated_manifest.get("intent_count", 0)
    )
    generated_viewpoints = generated_csv.get(
        "viewpoints", _generated_viewpoints(generated_manifest)
    )
    generated_conditions = generated_csv.get(
        "condition_combinations", []
    )
    generated_boundary = generated_csv.get(
        "boundary_domain", _generated_boundary(generated_manifest)
    )
    generated_stub = generated_csv.get(
        "stub", _generated_stub(generated_manifest)
    )
    generated_oracle = generated_csv.get(
        "oracle", _generated_oracle(generated_manifest)
    )
    generated_projection = generated_csv.get(
        "projection", _generated_projection(generated_manifest)
    )
    generated_required_values = generated_csv.get(
        "required_values", _generated_required_values(generated_manifest)
    )
    return {
        "testcase_count": _dimension(generated_count, golden["testcase_count"]),
        "viewpoint": _dimension(generated_viewpoints, golden["viewpoints"]),
        "condition_combination": _dimension(
            generated_conditions, golden["condition_combinations"]
        ),
        "boundary_domain": _dimension(
            generated_boundary, golden["boundary_domain"]
        ),
        "stub": _dimension(generated_stub, golden["stub"]),
        "oracle": _dimension(generated_oracle, golden["oracle"]),
        "required_values": _dimension(
            generated_required_values, golden["required_values"]
        ),
        "projection": _dimension(generated_projection, golden["projection"]),
    }


def _generation_gap_category(generated_manifest: dict[str, Any]) -> str:
    text = " ".join(generated_manifest.get("issues", [])).lower()
    status = str(generated_manifest.get("status", "")).upper()
    if "unsupported" in text or status == "UNSUPPORTED":
        return "FUNCTION_IR_GAP"
    if "solver" in text or any(
        key not in {"SAT", "sat"}
        for key in generated_manifest.get("solve_statuses", {})
    ):
        return "SOLVER_GAP"
    if "evaluator" in text:
        return "EVALUATOR_GAP"
    if "oracle" in text:
        return "ORACLE_GAP"
    if "project rule" in text or "profile" in text:
        return "PROJECT_RULE_GAP"
    if status in {"NEEDS_REVIEW", "INVALID"}:
        return "SUITE_GAP"
    return "OBLIGATION_GAP"


def _gap(
    *,
    function: str,
    category: str,
    owner_layer: str,
    dimension: str,
    detail: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "owner_layer": owner_layer,
        "function": function,
        "dimension": dimension,
        "detail": detail,
        "review_required": True,
        "triage_owner": "wan37",
        "evidence": evidence or {},
    }


def _case_kinds_compatible(golden_kind: str, generated_kind: str) -> bool:
    if golden_kind == generated_kind:
        return True
    return (
        (golden_kind == "condition_combination"
         and generated_kind == "condition_outcome")
        or (golden_kind == "condition_outcome"
            and generated_kind == "condition_combination")
        or (golden_kind == "unlabelled"
            and generated_kind in {"execution", "loop"})
    )


def _case_gap_category(
    record: dict[str, Any], generated_manifest: dict[str, Any],
    generated_cases: list[dict[str, Any]], golden_cases: list[dict[str, Any]],
    project_evidence: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Classify one case from pipeline evidence, not from count direction."""
    project_evidence = project_evidence or {}
    match_type = record.get("match_type")
    if match_type == PARTIAL_MATCH:
        evidence = record.get("evidence", {})
        if (evidence.get("stub_mismatches")
                and not evidence.get("oracle_mismatches")):
            return "HARNESS_GAP", "stub/pre-state evidence differs"
        if (evidence.get("oracle_mismatches")
                or evidence.get("required_expected_mismatches")):
            return "ORACLE_GAP", "required post-state/oracle evidence differs"
        if evidence.get("required_input_mismatches"):
            return "BASELINE_GAP", (
                "reviewed required input values are not represented by the "
                "generated witness"
            )
        if (not evidence.get("truth_vector_equal")
                or not evidence.get("label_equal")):
            return "OBLIGATION_GAP", "viewpoint identity is only partially matched"
        return "SUITE_GAP", "case identity matched but suite evidence differs"
    if match_type == MISSING_GENERATED:
        if str(generated_manifest.get("status", "")).upper() != "VALIDATED":
            return _generation_gap_category(generated_manifest), (
                "generated suite did not pass its validation gate"
            )
        golden_identity = record.get("evidence", {}).get("golden", {})
        golden_kind = str(golden_identity.get("kind", ""))
        compatible = [
            case for case in generated_cases
            if _case_kinds_compatible(golden_kind, str(case.get("kind", "")))
        ]
        if not compatible:
            if (golden_kind == "condition_combination"
                    and project_evidence.get("mcdc_enabled") is False):
                return "PROJECT_RULE_GAP", "project MC/DC switch is disabled"
            if (golden_kind == "switch_case"
                    and project_evidence.get("switch_preserve_cases") is False):
                return "BASELINE_GAP", "approved switch case policy is disabled"
            return "OBLIGATION_GAP", (
                "no generated obligation has the Golden viewpoint kind"
            )
        statuses = {str(key).upper() for key in
                    generated_manifest.get("solve_statuses", {})}
        if statuses - {"SAT"}:
            return "SOLVER_GAP", "matching obligation has a non-SAT solve status"
        if (generated_manifest.get("evaluation_complete_count", 0)
                < generated_manifest.get("validated_intent_count", 0)):
            return "EVALUATOR_GAP", "generated evaluation is incomplete"
        if not generated_manifest.get("expected_keys"):
            return "ORACLE_GAP", "generated suite has no proven oracle columns"
        return "SUITE_GAP", (
            "matching obligation exists but no unique generated suite case "
            "was available"
        )
    if match_type == EXTRA_GENERATED:
        provenance = record.get("evidence", {}).get("provenance", {})
        if provenance.get("rule_id"):
            return "GOLDEN_ERROR", (
                "generated case is backed by an approved rule but has no "
                "reviewed Golden counterpart"
            )
        return "SUITE_GAP", "generated case has no approved viewpoint provenance"
    if match_type == AMBIGUOUS_MATCH:
        return "SUITE_GAP", "more than one generated case has the same best score"
    return "GOLDEN_ERROR", "unrecognized case comparison state"


def _case_gaps(
    *, function: str, case_matching: dict[str, Any],
    generated_manifest: dict[str, Any], generated_cases: list[dict[str, Any]],
    golden_cases: list[dict[str, Any]],
    project_evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in case_matching.get("records", []):
        if record.get("match_type") in {
                EXACT_SEMANTIC_MATCH, EQUIVALENT_REPRESENTATIVE}:
            continue
        category, reason = _case_gap_category(
            record, generated_manifest, generated_cases, golden_cases,
            project_evidence,
        )
        result.append(_gap(
            function=function, category=category, owner_layer=_CATEGORY_OWNERS[category],
            dimension=f"case:{record.get('golden_case_id') or record.get('generated_case_id')}",
            detail=f"{record.get('match_type')}: {reason}",
            evidence={
                "match": record,
                "classification_basis": reason,
                "project": project_evidence or {},
            },
        ))
    if not case_matching.get("row_count_equal", True):
        result.append(_gap(
            function=function, category="SUITE_GAP",
            owner_layer=_CATEGORY_OWNERS["SUITE_GAP"], dimension="row_count",
            detail=(
                "generated TestCsv row count differs from Golden: "
                f"generated={case_matching.get('generated_valid_case_count', 0)} "
                f"golden={case_matching.get('golden_case_count', 0)}"
            ),
            evidence={
                "generated": case_matching.get("generated_valid_case_count", 0),
                "golden": case_matching.get("golden_case_count", 0),
            },
        ))
    structural_order_equal = case_matching.get(
        "structural_row_order_equal", case_matching.get("row_order_equal", True)
    )
    if not structural_order_equal:
        result.append(_gap(
            function=function, category="SUITE_GAP",
            owner_layer=_CATEGORY_OWNERS["SUITE_GAP"], dimension="row_order",
            detail="generated TestCsv row order differs from Golden",
            evidence={
                "row_order": case_matching.get("row_order", {}),
                "structural_row_order": case_matching.get(
                    "structural_row_order", {}
                ),
            },
        ))
    return result


def _case_equivalence(case_matching: dict[str, Any]) -> str:
    counts = case_matching.get("counts", {})
    if counts.get(AMBIGUOUS_MATCH):
        return AMBIGUOUS_MATCH
    if counts.get(MISSING_GENERATED):
        return MISSING_GENERATED
    if counts.get(EXTRA_GENERATED):
        return EXTRA_GENERATED
    if counts.get(PARTIAL_MATCH):
        return PARTIAL_MATCH
    if counts.get(EQUIVALENT_REPRESENTATIVE):
        return EQUIVALENT_REPRESENTATIVE
    if not case_matching.get("row_count_equal", True):
        return "ROW_COUNT_DIFFERENCE"
    if not case_matching.get("row_order_equal", True):
        return "ROW_ORDER_DIFFERENCE"
    return EXACT_SEMANTIC_MATCH


def compare_function_semantics(
    *,
    function: str,
    generated_manifest: dict[str, Any],
    generated_csv: dict[str, Any] | None,
    golden: dict[str, Any] | None,
    actual_csv_path: Path,
    golden_csv_path: Path | None,
    generated_cases: list[dict[str, Any]] | None = None,
    generated_csv_cases: list[dict[str, Any]] | None = None,
    golden_cases: list[dict[str, Any]] | None = None,
    project_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare one function and classify every observable mismatch."""
    if golden is None:
        return {
            "equivalence": "GOLDEN_MISSING",
            "dimensions": {},
            "gaps": [_gap(
                function=function, category="GOLDEN_ERROR", owner_layer="golden",
                dimension="golden_file", detail="未找到唯一 reviewed Golden TestCsv",
                evidence={"golden_path": str(golden_csv_path) if golden_csv_path else None},
            )],
        }
    case_matching = None
    if (golden_cases is not None
            and (generated_cases is not None or generated_csv_cases is not None)):
        case_matching = match_semantic_cases(
            generated_csv_cases if generated_csv_cases is not None else generated_cases,
            golden_cases,
        )
    if generated_manifest.get("status") != "VALIDATED":
        category = _generation_gap_category(generated_manifest)
        dimensions = _comparison_dimensions(generated_manifest, generated_csv, golden)
        gaps = [_gap(
            function=function, category=category,
            owner_layer=_CATEGORY_OWNERS[category], dimension="generation_status",
            detail="生成结果未通过 VALIDATED 门禁",
            evidence={
                "status": generated_manifest.get("status"),
                "issues": generated_manifest.get("issues", []),
            },
        )]
        if case_matching is not None:
            gaps.extend(_case_gaps(
                function=function, case_matching=case_matching,
                generated_manifest=generated_manifest,
                generated_cases=generated_cases or [],
                golden_cases=golden_cases or [],
                project_evidence=project_evidence,
            ))
        return {
            "equivalence": "GENERATION_GATE_FAILED",
            "dimensions": dimensions,
            "case_matching": case_matching,
            "gaps": gaps,
        }
    if generated_csv is None:
        dimensions = _comparison_dimensions(generated_manifest, generated_csv, golden)
        return {
            "equivalence": "GENERATED_CSV_MISSING",
            "dimensions": dimensions,
            "case_matching": case_matching,
            "gaps": [_gap(
                function=function, category="PROJECTION_GAP",
                owner_layer=_CATEGORY_OWNERS["PROJECTION_GAP"],
                dimension="projection",
                detail="生成 manifest 已 VALIDATED 但 TestCsv 不存在或无法解析",
                evidence={"actual_path": str(actual_csv_path)},
            )],
        }

    dimensions = _comparison_dimensions(generated_manifest, generated_csv, golden)
    if case_matching is not None:
        gaps = _case_gaps(
            function=function, case_matching=case_matching,
            generated_manifest=generated_manifest,
            generated_cases=generated_cases or [],
            golden_cases=golden_cases or [],
            project_evidence=project_evidence,
        )
        projection = dimensions.get("projection", {})
        if projection.get("status") != "equal":
            gaps.append(_gap(
                function=function, category="PROJECTION_GAP",
                owner_layer=_CATEGORY_OWNERS["PROJECTION_GAP"],
                dimension="projection",
                detail="生成目标投影与 reviewed Golden 的列/场景结构不同",
                evidence={"generated": projection.get("generated"),
                          "golden": projection.get("golden")},
            ))
        return {
            "equivalence": _case_equivalence(case_matching),
            "dimensions": dimensions,
            "case_matching": case_matching,
            "gaps": gaps,
        }

    all_equal = all(item["status"] == "equal" for item in dimensions.values())
    exact = False
    if all_equal and actual_csv_path.is_file() and golden_csv_path and golden_csv_path.is_file():
        try:
            exact = semantic_csv_signature(actual_csv_path) == semantic_csv_signature(
                golden_csv_path
            )
        except (OSError, UnicodeError, ValueError):
            exact = False
    if exact:
        equivalence = "EXACT_SEMANTIC"
    elif all_equal:
        equivalence = "FREE_REPRESENTATIVE_EQUIVALENT"
    else:
        equivalence = "SEMANTIC_DIFFERENCE"

    gaps = []
    for name, item in dimensions.items():
        status = item["status"]
        if status == "equal":
            continue
        if name == "projection":
            category = "PROJECTION_GAP"
        elif name in {"oracle", "required_values"}:
            category = "ORACLE_GAP"
        elif name == "stub":
            category = "HARNESS_GAP"
        elif status == "missing_generated":
            category = "BASELINE_GAP"
        elif status == "extra_generated":
            category = "GOLDEN_ERROR"
        else:
            category = "OBLIGATION_GAP"
        gaps.append(_gap(
            function=function, category=category,
            owner_layer=_CATEGORY_OWNERS[category], dimension=name,
            detail=f"{name} semantic dimension differs ({status})",
            evidence={"generated": item["generated"], "golden": item["golden"]},
        ))
    return {"equivalence": equivalence, "dimensions": dimensions,
            "case_matching": None, "gaps": gaps}


def _safe_normalize(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return normalize_golden_csv(path), None
    except (OSError, UnicodeError, ValueError) as exc:
        return None, str(exc)


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _without_paths(value: Any) -> Any:
    """Remove machine-specific artifact paths before fingerprinting evidence."""
    if isinstance(value, dict):
        return {
            str(key): _without_paths(item)
            for key, item in value.items()
            if str(key) not in {
                "path", "actual_path", "golden_path", "testcsv",
                "intent_manifest", "project_corpus_manifest",
                "baseline_document",
            }
        }
    if isinstance(value, list):
        return [_without_paths(item) for item in value]
    if isinstance(value, tuple):
        return [_without_paths(item) for item in value]
    return value


def build_corpus_validation_report(
    manifest: ProjectCorpusManifest,
    context: Any,
    units: tuple[Any, ...],
    *,
    output_root: Path,
    generator_commit: str = "unknown",
    generator_version: str = "0.1.0",
    blocked: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Build a machine-readable all-functions generation/compare report."""
    function_reports: list[dict[str, Any]] = []
    all_gaps: list[dict[str, Any]] = []
    project_evidence = {
        "mcdc_enabled": bool(
            getattr(getattr(context, "manifest", None), "profile", {}).get(
                "mcdc_enabled", False
            )
        ),
        "baseline_ref": getattr(context, "baseline_ref", "unknown"),
        "switch_preserve_cases": bool(
            getattr(getattr(context, "baseline", None), "switch_policy", {}).get(
                "preserve_cases", False
            )
        ),
    }
    for item in sorted(blocked, key=lambda value: (
            int(value.get("row") or 0), str(value.get("function") or ""))):
        function = str(item.get("function") or "<blocked-project-fixture>")
        blocked_gap = _gap(
            function=function, category="FUNCTION_IR_GAP",
            owner_layer=_CATEGORY_OWNERS["FUNCTION_IR_GAP"],
            dimension="fixture",
            detail=f"项目校验被阻断: {item.get('detail', item.get('reason', 'unknown'))}",
            evidence={
                "status": "BLOCKED",
                "reason": item.get("reason", "FIXTURE_MISSING"),
                "source_path": item.get("source_path"),
            },
        )
        all_gaps.append(blocked_gap)
        function_reports.append({
            "row": item.get("row"), "function": function,
            "source": item.get("source_path"),
            "target_rel": item.get("target_rel"),
            "blocked": {
                "status": "BLOCKED", "reason": item.get("reason"),
                "detail": item.get("detail"),
            },
            "generated": {
                "status": "BLOCKED", "testcsv": None,
                "intent_manifest": None, "semantics": None,
                "csv_semantics": None,
            },
            "golden": {
                "status": item.get(
                    "reason", "FIXTURE_MISSING"
                ), "availability": item.get(
                    "reason", "FIXTURE_MISSING"
                ), "status_code": item.get("reason", "FIXTURE_MISSING"),
                "path": None, "semantics": None, "error": None,
            },
            "comparison": {
                "equivalence": "BLOCKED", "dimensions": {},
                "case_matching": None, "gaps": [blocked_gap],
            },
        })
    for unit in sorted(units, key=lambda item: (
            int(getattr(item.row, "row_number", 0)), str(item.row.function))):
        golden_path = golden_for_unit(manifest, unit)
        golden = None
        golden_error = None
        if golden_path is not None:
            golden, golden_error = _safe_normalize(golden_path)
        generated_error = None
        try:
            generated_manifest = normalize_generated_manifest(unit.intent_manifest)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            generated_error = str(exc)
            generated_manifest = {
                "status": str(unit.status), "intent_count": 0,
                "validated_intent_count": 0, "obligation_kinds": {},
                "outcomes": {}, "boundary_classes": {}, "pair_count": 0,
                "solve_statuses": {}, "evaluation_count": 0,
                "evaluation_complete_count": 0, "input_keys": [],
                "expected_keys": [], "stub_keys": [], "issues": [],
            }
        actual_csv = None
        actual_error = None
        if unit.testcsv.is_file():
            actual_csv, actual_error = _safe_normalize(unit.testcsv)
        generated_cases: list[dict[str, Any]] = []
        if generated_error is None:
            try:
                generated_cases = normalize_generated_cases(unit.intent_manifest)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                generated_error = str(exc)
        golden_cases: list[dict[str, Any]] = []
        if golden is not None:
            golden_cases = normalize_golden_cases(
                golden, source_path=golden_path,
            )
        generated_csv_cases: list[dict[str, Any]] | None = None
        if actual_csv is not None:
            generated_csv_cases = normalize_golden_cases(
                actual_csv, source_path=unit.testcsv,
            )
        comparison = compare_function_semantics(
            function=unit.row.function,
            generated_manifest=generated_manifest,
            generated_csv=actual_csv,
            golden=golden,
            actual_csv_path=unit.testcsv,
            golden_csv_path=golden_path,
            generated_cases=generated_cases,
            generated_csv_cases=generated_csv_cases,
            golden_cases=golden_cases,
            project_evidence=project_evidence,
        )
        if generated_error:
            comparison["gaps"].insert(0, _gap(
                function=unit.row.function, category="SUITE_GAP",
                owner_layer=_CATEGORY_OWNERS["SUITE_GAP"],
                dimension="generation_manifest",
                detail=f"生成 intent manifest 无法解析: {generated_error}",
                evidence={"error": generated_error},
            ))
        if golden_error:
            comparison["gaps"] = [_gap(
                function=unit.row.function, category="GOLDEN_ERROR",
                owner_layer=_CATEGORY_OWNERS["GOLDEN_ERROR"],
                dimension="golden_file",
                detail=f"Golden 解析失败: {golden_error}",
                evidence={"golden_path": str(golden_path)},
            )]
            comparison["equivalence"] = "GOLDEN_INVALID"
        if actual_error and generated_manifest.get("status") == "VALIDATED":
            comparison["gaps"].append(_gap(
                function=unit.row.function, category="PROJECTION_GAP",
                owner_layer=_CATEGORY_OWNERS["PROJECTION_GAP"],
                dimension="projection",
                detail=f"生成 TestCsv 解析失败: {actual_error}",
                evidence={"actual_path": str(unit.testcsv)},
            ))
        all_gaps.extend(comparison["gaps"])
        function_reports.append({
            "row": unit.row.row_number,
            "function": unit.row.function,
            "source": str(unit.row.source_path),
            "target_rel": unit.row.target_rel.as_posix(),
            "generated": {
                "status": unit.status,
                "testcsv": str(unit.testcsv),
                "intent_manifest": str(unit.intent_manifest),
                "semantics": generated_manifest,
                "csv_semantics": actual_csv,
            },
            "golden": {
                "status": "VALID" if golden is not None else
                "GOLDEN_INVALID" if golden_error else "GOLDEN_MISSING",
                "availability": "VALID" if golden is not None else
                "GOLDEN_INVALID" if golden_error else "GOLDEN_MISSING",
                "status_code": "VALID" if golden is not None else
                "GOLDEN_INVALID" if golden_error else "GOLDEN_MISSING",
                "path": str(golden_path) if golden_path else None,
                "semantics": golden,
                "error": golden_error,
            },
            "comparison": comparison,
        })

    function_reports.sort(key=lambda item: (
        int(item.get("row") or 0), str(item.get("function", ""))
    ))
    generation_statuses = _counts([
        str(item["generated"]["status"]) for item in function_reports
    ])
    equivalences = _counts([
        str(item["comparison"]["equivalence"]) for item in function_reports
    ])
    gap_categories = _counts([str(item["category"]) for item in all_gaps])
    extraction = {}
    extraction_report = Path(output_root) / "index-generation-report.json"
    if extraction_report.is_file():
        try:
            extraction = _read_json(extraction_report).get("extraction", {})
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            extraction = {"status": "unreadable"}

    stable_functions = [{
        "function": item["function"],
        "generated_status": item["generated"]["status"],
        "generated": _without_paths(item["generated"]["semantics"]),
        "generated_csv": _without_paths(item["generated"]["csv_semantics"]),
        "golden": _without_paths(item["golden"]["semantics"]),
        "equivalence": item["comparison"]["equivalence"],
        "dimensions": item["comparison"]["dimensions"],
        "case_matching": _without_paths(
            item["comparison"].get("case_matching")
        ),
        "gaps": [
            {"category": gap["category"], "dimension": gap["dimension"]}
            for gap in item["comparison"]["gaps"]
        ],
    } for item in function_reports]
    projection_stable = [{
        "function": item["function"],
        "generated": (item["generated"]["csv_semantics"] or {}).get("projection"),
        "golden": (item["golden"]["semantics"] or {}).get("projection"),
    } for item in function_reports]
    provenance = dict(getattr(context, "provenance", {}))
    provenance.update({
        "generator_commit": generator_commit,
        "generator_version": generator_version,
        "project_corpus_manifest": str(manifest.path),
        "baseline_document": str(manifest.baseline_document),
        "baseline_sheet": manifest.baseline_sheet,
        "baseline_revision": manifest.baseline_revision,
    })
    case_match_counts = Counter()
    for item in function_reports:
        matching = item["comparison"].get("case_matching") or {}
        case_match_counts.update(matching.get("counts", {}))
    case_match_counts = dict(sorted(case_match_counts.items()))
    golden_availability = [
        item["golden"].get("availability", item["golden"].get("status"))
        for item in function_reports
    ]
    golden_valid = sum(status == "VALID" for status in golden_availability)
    golden_missing = sum(status == "GOLDEN_MISSING" for status in golden_availability)
    golden_invalid = sum(status == "GOLDEN_INVALID" for status in golden_availability)
    golden_testcases = sum(
        int((item["golden"]["semantics"] or {}).get("testcase_count", 0))
        for item in function_reports
    )
    generated_intents = sum(
            int((item["generated"]["semantics"] or {}).get("intent_count", 0))
        for item in function_reports
    )
    generated_testcases = sum(
        int((item["generated"]["csv_semantics"] or {}).get("testcase_count", 0))
        for item in function_reports
    )
    return {
        "schema_version": 1,
        "report_kind": "project-validation",
        "status": "PASS" if units and not all_gaps else "REVIEW_REQUIRED",
        "project": {
            "id": manifest.project_id,
            "baseline": getattr(context, "baseline_ref", "unknown"),
            "scope": manifest.scope,
            "function_count": len(function_reports),
            "indexed_function_count": len(function_reports),
        },
        "provenance": provenance,
        "extraction": extraction,
        "totals": {
            "functions": len(function_reports),
            "indexed_functions": len(function_reports),
            "processed_functions": sum(
                item["generated"]["status"] != "BLOCKED"
                for item in function_reports
            ),
            "blocked_functions": sum(
                item["generated"]["status"] == "BLOCKED"
                or item["golden"].get("availability") in {
                    "GOLDEN_MISSING", "GOLDEN_INVALID", "FIXTURE_MISSING"
                }
                for item in function_reports
            ),
            "generation_statuses": generation_statuses,
            "golden_valid": golden_valid,
            "golden_available": golden_valid,
            "golden_missing": golden_missing,
            "golden_invalid": golden_invalid,
            "golden_missing_or_invalid": golden_missing + golden_invalid,
            "equivalences": equivalences,
            "golden_testcases": golden_testcases,
            "human_testcase_count": golden_testcases,
            "generated_intents": generated_intents,
            "generated_testcases": generated_testcases,
            "generated_testcase_count": generated_testcases,
            "case_matches": case_match_counts,
            "gap_count": len(all_gaps),
            "gap_categories": gap_categories,
            "unclassified_mismatches": 0,
        },
        "taxonomy": {
            "categories": list(STANDARD_GAP_CATEGORIES),
            "owner_layers": dict(_CATEGORY_OWNERS),
            "dimension_owners": dict(_DIMENSION_OWNERS),
            "review_policy": (
                "count direction never selects a root cause; uncertain case "
                "evidence remains a review gap for wan37"
            ),
        },
        "functions": function_reports,
        "stability": {
            "semantic_fingerprint": _digest(stable_functions),
            "projection_fingerprint": _digest(projection_stable),
            "inputs_excluded_from_fingerprint": [
                "output paths", "generator commit"
            ],
        },
    }


def write_corpus_validation_report(report: dict[str, Any], path: Path) -> Path:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def render_project_validation_markdown(report: dict[str, Any]) -> str:
    """Render a concise deterministic companion to project-validation.json."""
    project = report.get("project", {})
    totals = report.get("totals", {})
    lines = [
        f"# Project validation: {project.get('id', 'unknown')}",
        "",
        f"- Status: `{report.get('status', 'UNKNOWN')}`",
        f"- Baseline: `{project.get('baseline', 'unknown')}`",
        f"- Scope: `{project.get('scope', 'unknown')}`",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
        f"| Indexed functions | {totals.get('indexed_functions', 0)} |",
        f"| Processed functions | {totals.get('processed_functions', 0)} |",
        f"| Golden testcases | {totals.get('human_testcase_count', 0)} |",
        f"| Generated testcases | {totals.get('generated_testcase_count', 0)} |",
        f"| Gaps | {totals.get('gap_count', 0)} |",
        "",
        "## Case matching",
        "",
        "| Match type | Count |",
        "| --- | ---: |",
    ]
    for name, count in sorted((totals.get("case_matches", {}) or {}).items()):
        lines.append(f"| `{name}` | {count} |")
    lines.extend(["", "## Functions", "", "| Function | Generated | Golden | Equivalence | Gaps |",
                  "| --- | --- | --- | --- | ---: |"])
    for item in report.get("functions", []):
        comparison = item.get("comparison", {})
        lines.append(
            f"| `{item.get('function', '')}` | "
            f"`{item.get('generated', {}).get('status', 'UNKNOWN')}` | "
            f"`{item.get('golden', {}).get('status', 'UNKNOWN')}` | "
            f"`{comparison.get('equivalence', 'UNKNOWN')}` | "
            f"{len(comparison.get('gaps', []))} |"
        )
    lines.extend(["", "## Gap categories", "", "| Category | Count |", "| --- | ---: |"])
    for name, count in sorted((totals.get("gap_categories", {}) or {}).items()):
        lines.append(f"| `{name}` | {count} |")
    return "\n".join(lines) + "\n"


def write_project_validation_markdown(report: dict[str, Any], path: Path) -> Path:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_project_validation_markdown(report),
        encoding="utf-8", newline="\n",
    )
    return path


__all__ = [
    "ProjectCorpusManifest", "STANDARD_GAP_CATEGORIES",
    "build_corpus_validation_report", "compare_function_semantics",
    "golden_for_unit", "load_corpus_manifest", "normalize_generated_manifest",
    "preflight_corpus",
    "validate_corpus_paths", "write_corpus_validation_report",
    "render_project_validation_markdown", "write_project_validation_markdown",
]
