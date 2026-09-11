"""Deterministic project-corpus validation and semantic gap reporting.

This module is deliberately downstream of generation.  It reads the explicit
project index, generated FunctionIR/test-intent/CSV artifacts, and reviewed
Golden CSVs; it never supplies Golden data to the generator or oracle.
"""
from __future__ import annotations

import csv
import concurrent.futures
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ut_agent.learning.golden import normalize_golden_csv, semantic_csv_signature
from ut_agent.observability import ProgressRecorder
from .cases import (
    AMBIGUOUS_MATCH, EXACT_SEMANTIC_MATCH, EQUIVALENT_REPRESENTATIVE,
    EXTRA_GENERATED, MISSING_GENERATED, PARTIAL_MATCH,
    match_semantic_cases, normalize_generated_cases, normalize_golden_cases,
)


STANDARD_GAP_CATEGORIES = (
    "RUN_INCOMPLETE",
    "REPORTING_LIMIT",
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

# Issue #12 distinguishes the first observed pipeline owner from the
# calibration conclusion.  A semantic comparison can establish the former,
# but it must not infer the latter from Golden/count evidence alone.
CALIBRATION_CLASSIFICATIONS = (
    "FUNCTION_IR_GAP",
    "IMPLEMENTATION_DRIFT",
    "RUNTIME_MAPPING_DRIFT",
    "BASELINE_INTERPRETATION_DRIFT",
    "PROJECT_RULE_GAP",
    "GOLDEN_ERROR",
    "NORMATIVE_RULE_GAP",
    "NEEDS_REVIEW",
)

SEMANTIC_MATCH_PAIR_BUDGET = int(
    os.environ.get("UT_AGENT_MATCH_PAIR_BUDGET", "4096")
)
SEMANTIC_MATCH_INTENT_BYTES_BUDGET = int(
    os.environ.get("UT_AGENT_INTENT_BYTES_BUDGET", str(1024 * 1024))
)

_CALIBRATION_INVESTIGATION_ORDER = (
    ("FunctionIR", "FUNCTION_IR_GAP"),
    ("implementation", "IMPLEMENTATION_DRIFT"),
    ("runtime mapping", "RUNTIME_MAPPING_DRIFT"),
    ("baseline interpretation", "BASELINE_INTERPRETATION_DRIFT"),
    ("project rule", "PROJECT_RULE_GAP"),
    ("reviewed Golden", "GOLDEN_ERROR"),
    ("normative rule", "NORMATIVE_RULE_GAP"),
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
    "RUN_INCOMPLETE": "orchestration/reporting",
    "REPORTING_LIMIT": "orchestration/reporting",
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


def _indexed_records(manifest: ProjectCorpusManifest) -> list[dict[str, Any]]:
    """Read only the index identity needed to account for partial runs."""
    records: list[dict[str, Any]] = []
    with manifest.index_csv.open("r", encoding="cp932", newline="") as stream:
        for row_number, values in enumerate(csv.reader(stream), 1):
            if not values or all(not value.strip() for value in values):
                continue
            function = values[2].strip() if len(values) > 2 else None
            target_rel = None
            if len(values) >= 5:
                # Keep every path component below the configured golden root.
                # N-O2606 stores ``WinAMS\src\...`` while the manifest already
                # points at ``...\WinAMS``; stripping ``src`` here falsely
                # turns every existing Golden into a missing file.
                target_rel = _index_tail(values[4], ("WinAMS",))
                if target_rel is None:
                    target_rel = _index_tail(values[4], ("winAMS",))
            records.append({
                "row": row_number,
                "function": function,
                "target_rel": target_rel.as_posix() if target_rel else None,
            })
    return records


def _golden_for_target(
    manifest: ProjectCorpusManifest, target_rel: str | Path | None, function: str,
) -> Path | None:
    """Resolve one reviewed Golden from the index target mapping.

    The project index owns the target directory mapping.  A single filename
    suffix such as ``...ramdf1.csv`` is accepted only when it is unambiguous;
    no testcase rows or values are inferred from the Golden.
    """
    if target_rel is None:
        return None
    target_dir = manifest.golden_root / Path(target_rel) / "TestCsv"
    exact = target_dir / f"{function}.csv"
    if exact.is_file():
        return exact
    if not target_dir.is_dir():
        return None
    prefix = function.lower()
    candidates = sorted(
        (item for item in target_dir.iterdir()
         if item.is_file() and item.suffix.lower() == ".csv"
         and item.stem.lower().startswith(prefix)),
        key=lambda item: item.name.lower(),
    )
    return candidates[0] if len(candidates) == 1 else None


def golden_for_unit(manifest: ProjectCorpusManifest, unit: Any) -> Path | None:
    target_rel = Path(unit.row.target_rel)
    # ``load_index`` keeps the renderer's target relative to its target base:
    # Product packages use ``WinAMS/src`` while Soft packages use ``winAMS``.
    # The corpus manifest points at the target root itself, so carry only the
    # path below the base marker into the Golden lookup.
    target_base = getattr(unit.row, "target_base_rel", None)
    if target_base is not None:
        parts = list(Path(target_base).parts)
        marker = next(
            (index for index, part in enumerate(parts)
             if str(part).lower() == "winams"),
            None,
        )
        if marker is not None:
            prefix = Path(*parts[marker + 1:])
            target_rel = prefix / target_rel
    return _golden_for_target(manifest, target_rel, unit.row.function)


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


def _generation_gate_manifest(unit: Any) -> dict[str, Any]:
    """Describe a failed unit without deserializing its large intent payload.

    The target projection already returns ``generation_status``.  When that
    gate is not VALIDATED, per-intent metrics and CSV semantics are downstream
    observations, so reading a potentially very large intent document adds no
    adjudicable evidence to an Issue #12 root-cause report.
    """
    result = {
        "status": str(getattr(unit, "generation_status", getattr(unit, "status", "UNKNOWN"))),
        "csv_written": bool(getattr(unit, "testcsv", None)
                            and unit.testcsv.is_file()),
        "csv_kind": "not_inspected_generation_gate",
        "csv_intent_count": None,
        "intent_count": None,
        "validated_intent_count": None,
        "obligation_kinds": None,
        "outcomes": None,
        "boundary_classes": None,
        "pair_count": None,
        "solve_statuses": None,
        "evaluation_count": None,
        "evaluation_complete_count": None,
        "input_keys": None,
        "expected_keys": None,
        "stub_keys": None,
        "issues": [],
        "obligation_count": None,
        "evaluation_statuses": {},
        "validation_statuses": None,
        "details_status": "SKIPPED_GENERATION_GATE",
    }
    error = getattr(unit, "error", None)
    if error:
        result["issues"].append(str(error))
    summary_path = getattr(unit, "intent_manifest", None)
    if summary_path:
        try:
            summary = _read_json(Path(summary_path).with_name("test-intents-summary.json"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            summary = None
        if isinstance(summary, dict):
            result.update(summary)
            result["details_status"] = "SUMMARY_ONLY_GENERATION_GATE"
    if not result["issues"]:
        result["issues"] = [
            "detailed intent evidence was not inspected because generation gate is not VALIDATED"
        ]
    return result


def _generated_viewpoints(manifest: dict[str, Any]) -> dict[str, Any]:
    kinds = manifest.get("obligation_kinds")
    return {"counts": dict(kinds) if isinstance(kinds, dict) else None, "labels": []}


def _generated_boundary(manifest: dict[str, Any]) -> dict[str, Any]:
    values = manifest.get("boundary_classes")
    return {
        "input_value_classes": {},
        "expected_value_classes": {},
        "all_value_classes": dict(values) if isinstance(values, dict) else None,
    }


def _generated_stub(manifest: dict[str, Any]) -> dict[str, Any]:
    values = manifest.get("stub_keys")
    return {
        "declaration_count": 0,
        "declarations": [],
        "columns": list(values) if isinstance(values, list) else None,
    }


def _generated_oracle(manifest: dict[str, Any]) -> dict[str, Any]:
    values = manifest.get("expected_keys")
    return {
        "output_count": len(values) if isinstance(values, list) else None,
        "columns": list(values) if isinstance(values, list) else None,
    }


def _generated_projection(manifest: dict[str, Any]) -> dict[str, Any]:
    inputs = manifest.get("input_keys")
    expected = manifest.get("expected_keys")
    return {
        "input_count": len(inputs) if isinstance(inputs, list) else None,
        "output_count": len(expected) if isinstance(expected, list) else None,
        "comment_columns": [],
        "observed_label_count": 0,
        "scenario_count": manifest.get("intent_count"),
    }


def _generated_required_values(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "inputs": [] if isinstance(manifest.get("input_keys"), list) else None,
        "expected": [] if isinstance(manifest.get("expected_keys"), list) else None,
    }


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {} or value == 0


def _dimension(generated: Any, golden: Any) -> dict[str, Any]:
    if generated is None or golden is None:
        status = "unknown"
    elif generated == golden:
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
    solve_statuses = generated_manifest.get("solve_statuses") or {}
    if "solver" in text or any(
        key not in {"SAT", "sat"} for key in solve_statuses
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
        "calibration": {
            "status": "PENDING",
            "classification": "NEEDS_REVIEW",
            "first_divergence_layer": None,
            "observed_pipeline_category": category,
            "golden_role": "detector_only",
            "required_checks": [
                {"layer": layer, "classification_if_first_divergence": result}
                for layer, result in _CALIBRATION_INVESTIGATION_ORDER
            ],
        },
    }


def _mark_derived_gaps(
    gaps: list[dict[str, Any]], root_gap: dict[str, Any],
) -> list[dict[str, Any]]:
    """Mark downstream observations without promoting them to root causes."""
    root = {
        "function": root_gap["function"],
        "category": root_gap["category"],
        "owner_layer": root_gap["owner_layer"],
        "dimension": root_gap["dimension"],
    }
    for gap in gaps:
        gap["derived_from"] = root
        gap["root_cause_status"] = "DERIVED"
    return gaps


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
    matching_budget: dict[str, Any] | None = None,
    project_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare one function and classify every observable mismatch."""
    if matching_budget is not None:
        dimensions = (
            _comparison_dimensions(generated_manifest, generated_csv, golden)
            if golden is not None else {}
        )
        budget_gap = _gap(
            function=function, category="REPORTING_LIMIT",
            owner_layer=_CATEGORY_OWNERS["REPORTING_LIMIT"],
            dimension="semantic_match_budget",
            detail=(
                "semantic case comparison exceeds the deterministic "
                "per-function pair budget"
            ),
            evidence=matching_budget,
        )
        budget_gap["root_cause_status"] = "REPORTING_LIMIT"
        return {
            "equivalence": "MATCHING_BUDGET_EXCEEDED",
            "dimensions": dimensions,
            "case_matching": matching_budget,
            "gaps": [budget_gap],
        }
    if generated_manifest.get("status") != "VALIDATED":
        category = _generation_gap_category(generated_manifest)
        dimensions = (
            _comparison_dimensions(generated_manifest, generated_csv, golden)
            if golden is not None else {}
        )
        gaps = [_gap(
            function=function, category=category,
            owner_layer=_CATEGORY_OWNERS[category], dimension="generation_status",
            detail="生成结果未通过 VALIDATED 门禁",
            evidence={
                "status": generated_manifest.get("status"),
                "issues": generated_manifest.get("issues", []),
            },
        )]
        gaps[0]["root_cause_status"] = "CANDIDATE_ROOT"
        return {
            "equivalence": "GENERATION_GATE_FAILED",
            "dimensions": dimensions,
            # A failed generator gate is the upstream evidence.  Matching
            # unvalidated cases against Golden rows would create a large set
            # of derived symptoms that cannot be adjudicated until the gate
            # itself is resolved.
            "case_matching": {
                "status": "SKIPPED_GENERATION_GATE",
                "reason": "generated suite is not VALIDATED",
            },
            "gaps": gaps,
        }
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
        equiv = _case_equivalence(case_matching)
        has_case_defects = bool(gaps) or equiv in {"MISSING_GENERATED", "EXTRA_GENERATED", "AMBIGUOUS_MATCH", "PARTIAL_MATCH"}
        if projection.get("status") != "equal" and has_case_defects:
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


def _physical_csv_row_upper_bound(path: Path) -> int:
    """Return a cheap, conservative row upper bound for match budgeting."""
    # This is resource-control metadata, not CSV semantic parsing.
    return Path(path).read_bytes().count(b"\n")


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


@dataclass(frozen=True)
class UnitValidationResult:
    row_number: int
    function_report: dict[str, Any]
    gaps: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]


def _process_single_unit_validation(
    unit: Any,
    manifest: ProjectCorpusManifest,
    project_evidence: dict[str, Any],
    pair_budget: int | None,
    bytes_budget: int | None,
    project_id: str,
    run_id: str,
) -> UnitValidationResult:
    events: list[dict[str, Any]] = []
    progress = ProgressRecorder(
        project=project_id,
        run_id=run_id,
        collector=events,
    )
    golden_path = golden_for_unit(manifest, unit)
    golden = None
    golden_error = None
    golden_file_status = "PRESENT" if golden_path is not None else "MISSING"
    golden_inspection_status = "NOT_INSPECTED"
    generated_error = None
    generation_status = str(getattr(
        unit, "generation_status", getattr(unit, "status", "UNKNOWN")
    ))
    with progress.stage(
        unit.row.function, "intent_normalization",
        {"intent_manifest": str(unit.intent_manifest)},
    ) as timing:
        if generation_status != "VALIDATED":
            generated_manifest = _generation_gate_manifest(unit)
            timing["status"] = str(generated_manifest.get("details_status", "SUMMARY_ONLY"))
        else:
            try:
                generated_manifest = _read_json(
                    Path(unit.intent_manifest).with_name(
                        "test-intents-summary.json"
                    )
                )
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                generated_error = str(exc)
                try:
                    generated_manifest = normalize_generated_manifest(
                        unit.intent_manifest
                    )
                    generated_error = None
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as fallback:
                    generated_error = f"summary: {exc}; full manifest: {fallback}"
                    generated_manifest = {
                        "status": generation_status, "intent_count": None,
                        "validated_intent_count": None, "obligation_kinds": None,
                        "outcomes": None, "boundary_classes": None, "pair_count": None,
                        "solve_statuses": None, "evaluation_count": None,
                        "evaluation_complete_count": None, "input_keys": None,
                        "expected_keys": None, "stub_keys": None, "issues": [],
                    }
            timing["metadata"].update({
                "details_status": generated_manifest.get("details_status", "SUMMARY_ONLY"),
                "intent_count": generated_manifest.get("intent_count"),
                "solve_statuses": generated_manifest.get("solve_statuses"),
            })
    matching_budget = None
    csv_only_intent_payload = False
    if generation_status == "VALIDATED" and golden_path is not None:
        try:
            golden_row_upper_bound = _physical_csv_row_upper_bound(golden_path)
        except OSError as exc:
            golden_error = str(exc)
            golden_row_upper_bound = 0
        intent_count = generated_manifest.get("intent_count")
        effective_pair_budget = (
            pair_budget if pair_budget is not None else SEMANTIC_MATCH_PAIR_BUDGET
        )
        effective_bytes_budget = (
            bytes_budget if bytes_budget is not None else SEMANTIC_MATCH_INTENT_BYTES_BUDGET
        )
        pair_upper_bound = (
            int(intent_count) * golden_row_upper_bound
            if isinstance(intent_count, int) else None
        )
        if (effective_pair_budget and pair_upper_bound is not None
                and pair_upper_bound > effective_pair_budget):
            matching_budget = {
                "status": "MATCHING_BUDGET_EXCEEDED",
                "pair_budget": effective_pair_budget,
                "candidate_pairs_upper_bound": pair_upper_bound,
                "generated_intent_count": intent_count,
                "golden_physical_row_upper_bound": golden_row_upper_bound,
                "budget_basis": "physical_csv_row_upper_bound",
            }
    else:
        effective_bytes_budget = (
            bytes_budget if bytes_budget is not None else SEMANTIC_MATCH_INTENT_BYTES_BUDGET
        )
    if (generation_status == "VALIDATED" and matching_budget is None
            and effective_bytes_budget
            and Path(unit.intent_manifest).is_file()
            and Path(unit.intent_manifest).stat().st_size
            > effective_bytes_budget):
        csv_only_intent_payload = True
        generated_manifest = dict(generated_manifest)
        generated_manifest.update({
            "details_status": "CSV_ONLY_INTENT_BYTES_BUDGET",
            "intent_payload_bytes": Path(unit.intent_manifest).stat().st_size,
            "intent_payload_bytes_budget": effective_bytes_budget,
        })
    with progress.stage(
        unit.row.function, "golden_parse",
        {"path": str(golden_path) if golden_path else None},
    ) as timing:
        if (generation_status == "VALIDATED" and matching_budget is None
                and golden_path is not None and golden_error is None):
            golden, golden_error = _safe_normalize(golden_path)
            golden_inspection_status = "PARSED" if golden is not None else "PARSE_FAILED"
        elif golden_path is None:
            golden_inspection_status = "NOT_APPLICABLE_MISSING"
            timing["status"] = "SKIPPED"
            timing["metadata"]["reason"] = "golden_file_missing_or_ambiguous"
        else:
            golden_inspection_status = "NOT_INSPECTED"
            timing["status"] = "SKIPPED"
            timing["metadata"]["reason"] = (
                "generation_gate" if generation_status != "VALIDATED"
                else "matching_budget"
            )
        timing["metadata"].update({
            "file_status": golden_file_status,
            "inspection_status": golden_inspection_status,
        })
    if (generation_status == "VALIDATED" and matching_budget is None
            and not csv_only_intent_payload and generated_error is None):
        try:
            generated_manifest = normalize_generated_manifest(
                unit.intent_manifest
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            generated_error = str(exc)
    actual_csv = None
    actual_error = None
    if (generation_status == "VALIDATED" and matching_budget is None
            and unit.testcsv.is_file()):
        actual_csv, actual_error = _safe_normalize(unit.testcsv)
    generated_cases: list[dict[str, Any]] = []
    if (generation_status == "VALIDATED" and matching_budget is None
            and not csv_only_intent_payload and generated_error is None):
        with progress.stage(
            unit.row.function, "intent_normalization_full",
            {"intent_manifest": str(unit.intent_manifest)},
        ) as timing:
            try:
                generated_cases = normalize_generated_cases(unit.intent_manifest)
                timing["metadata"]["case_count"] = len(generated_cases)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                generated_error = str(exc)
    else:
        with progress.stage(
            unit.row.function, "intent_normalization_full",
            {"intent_manifest": str(unit.intent_manifest)},
        ) as timing:
            timing["status"] = "SKIPPED"
            timing["metadata"]["reason"] = (
                "generation_gate" if generation_status != "VALIDATED"
                else "matching_budget_or_summary_error"
            )
    golden_cases: list[dict[str, Any]] = []
    if (generation_status == "VALIDATED" and matching_budget is None
            and golden is not None):
        golden_cases = normalize_golden_cases(
            golden, source_path=golden_path,
        )
    generated_csv_cases: list[dict[str, Any]] | None = None
    if (generation_status == "VALIDATED" and matching_budget is None
            and actual_csv is not None):
        generated_csv_cases = normalize_golden_cases(
            actual_csv, source_path=unit.testcsv,
        )
    with progress.stage(
        unit.row.function, "case_matching",
        {"golden_path": str(golden_path) if golden_path else None},
    ) as timing:
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
            matching_budget=matching_budget,
            project_evidence=project_evidence,
        )
        matching = comparison.get("case_matching") or {}
        if matching.get("status") in {
                "SKIPPED_GENERATION_GATE", "NOT_COMPARED", "MATCHING_BUDGET_EXCEEDED"}:
            timing["status"] = "SKIPPED"
            timing["metadata"]["reason"] = matching.get("status")
        timing["metadata"].update({
            "equivalence": comparison.get("equivalence"),
            "candidate_pairs": matching.get("candidate_pairs_upper_bound"),
            "matched_case_count": matching.get("matched_case_count"),
        })
    if csv_only_intent_payload and comparison.get("case_matching") is None:
        comparison["case_matching"] = {
            "status": "NOT_INSPECTED_CSV_ONLY",
            "reason": "full intent payload exceeded the declared byte budget",
        }
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
    golden_status = (
        "VALID" if golden is not None else
        "GOLDEN_INVALID" if golden_error else
        "PRESENT_NOT_INSPECTED" if golden_path is not None else
        "GOLDEN_MISSING"
    )
    target_rel_str = (
        unit.row.target_rel.as_posix()
        if hasattr(unit.row.target_rel, "as_posix")
        else str(unit.row.target_rel).replace("\\", "/")
    )
    report_item = {
        "row": unit.row.row_number,
        "function": unit.row.function,
        "source": str(unit.row.source_path),
        "target_rel": target_rel_str,
        "generated": {
            "status": unit.status,
            "testcsv": str(unit.testcsv),
            "intent_manifest": str(unit.intent_manifest),
            "semantics": generated_manifest,
            "csv_semantics": actual_csv,
        },
        "golden": {
            "status": golden_status,
            "availability": golden_file_status,
            "status_code": golden_status,
            "file_status": golden_file_status,
            "inspection_status": golden_inspection_status,
            "path": str(golden_path) if golden_path else None,
            "semantics": golden,
            "error": golden_error,
        },
        "comparison": comparison,
    }
    return UnitValidationResult(
        row_number=int(getattr(unit.row, "row_number", 0)),
        function_report=report_item,
        gaps=tuple(comparison["gaps"]),
        events=tuple(events),
    )


def _resolve_validation_workers(jobs: int | None, task_count: int) -> int:
    if task_count <= 1:
        return 1
    if jobs is not None:
        if jobs <= 0:
            count = os.cpu_count() or 4
            return max(1, min(count, task_count))
        return max(1, min(jobs, task_count))
    count = os.cpu_count() or 4
    return max(1, min(count, task_count))


def build_corpus_validation_report(
    manifest: ProjectCorpusManifest,
    context: Any,
    units: tuple[Any, ...],
    *,
    output_root: Path,
    generator_commit: str = "unknown",
    generator_version: str = "0.1.0",
    blocked: tuple[dict[str, Any], ...] = (),
    pair_budget: int | None = None,
    bytes_budget: int | None = None,
    jobs: int | None = None,
) -> dict[str, Any]:
    """Build a machine-readable all-functions generation/compare report."""
    progress = ProgressRecorder(
        Path(output_root) / "progress-events.jsonl",
        project=manifest.project_id,
        run_id=Path(output_root).name,
    )
    try:
        expected_records = _indexed_records(manifest)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        expected_records = []
        index_error = f"{type(exc).__name__}: {exc}"
    else:
        index_error = None
    function_reports: list[dict[str, Any]] = []
    all_gaps: list[dict[str, Any]] = []
    processed_rows: set[int] = set()
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
                "csv_semantics": None, "artifact_status": "MISSING",
            },
            "golden": {
                "status": item.get(
                    "reason", "FIXTURE_MISSING"
                ), "availability": item.get(
                    "reason", "FIXTURE_MISSING"
                ), "status_code": item.get("reason", "FIXTURE_MISSING"),
                "path": None, "semantics": None, "error": None,
                "file_status": "MISSING",
                "inspection_status": "NOT_APPLICABLE_MISSING",
            },
            "comparison": {
                "equivalence": "BLOCKED", "dimensions": {},
                "case_matching": {"status": "NOT_COMPARED"}, "gaps": [blocked_gap],
            },
        })
    sorted_units = sorted(units, key=lambda item: (
        int(getattr(item.row, "row_number", 0)), str(item.row.function)
    ))
    workers = _resolve_validation_workers(jobs, len(sorted_units))
    validation_results: list[UnitValidationResult] = []

    if workers == 1:
        for unit in sorted_units:
            validation_results.append(
                _process_single_unit_validation(
                    unit=unit,
                    manifest=manifest,
                    project_evidence=project_evidence,
                    pair_budget=pair_budget,
                    bytes_budget=bytes_budget,
                    project_id=manifest.project_id,
                    run_id=Path(output_root).name,
                )
            )
    else:
        tasks = [
            (
                unit,
                manifest,
                project_evidence,
                pair_budget,
                bytes_budget,
                manifest.project_id,
                Path(output_root).name,
            )
            for unit in sorted_units
        ]
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_process_single_unit_validation, *task) for task in tasks]
            for future in concurrent.futures.as_completed(futures):
                validation_results.append(future.result())

    # 确定性保证：严格按 row_number 升序排序
    validation_results.sort(key=lambda item: item.row_number)

    for item in validation_results:
        processed_rows.add(item.row_number)
        progress.record_events(item.events)
        all_gaps.extend(item.gaps)
        function_reports.append(item.function_report)

    extraction_report_data: dict[str, Any] = {}
    extraction_report = Path(output_root) / "index-generation-report.json"
    if extraction_report.is_file():
        try:
            extraction_report_data = _read_json(extraction_report)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            extraction_report_data = {"read_error": str(exc)}
    for record in expected_records:
        row_number = int(record["row"])
        if row_number in processed_rows:
            continue
        function = str(record.get("function") or "<unprocessed-index-row>")
        target_rel = record.get("target_rel")
        testcsv = (
            Path(output_root) / Path(target_rel) / "TestCsv" / f"{function}.csv"
            if target_rel else None
        )
        intent_manifest = (
            Path(output_root) / Path(target_rel) / "test-intents.json"
            if target_rel else None
        )
        golden_path = _golden_for_target(manifest, target_rel, function)
        reason = extraction_report_data.get("error") or (
            "indexed function was not returned by the generation run"
        )
        missing_gap = _gap(
            function=function,
            category="RUN_INCOMPLETE",
            owner_layer=_CATEGORY_OWNERS["RUN_INCOMPLETE"],
            dimension="indexed_function",
            detail=f"索引目标未完成处理: {reason}",
            evidence={
                "row": row_number,
                "target_rel": target_rel,
                "index_generation_report": str(extraction_report),
                "testcsv_exists": bool(testcsv and testcsv.is_file()),
                "intent_manifest_exists": bool(
                    intent_manifest and intent_manifest.is_file()
                ),
            },
        )
        missing_gap["root_cause_status"] = "CANDIDATE_ROOT"
        all_gaps.append(missing_gap)
        golden_file_status = "PRESENT" if golden_path is not None else "MISSING"
        function_reports.append({
            "row": row_number,
            "function": function,
            "source": None,
            "target_rel": target_rel,
            "generated": {
                "status": "NOT_PROCESSED",
                "artifact_status": "PRESENT_NOT_INSPECTED" if testcsv and testcsv.is_file() else "MISSING",
                "testcsv": str(testcsv) if testcsv else None,
                "intent_manifest": str(intent_manifest) if intent_manifest else None,
                "semantics": None,
                "csv_semantics": None,
            },
            "golden": {
                "status": "PRESENT_NOT_INSPECTED" if golden_path else "GOLDEN_MISSING",
                "availability": golden_file_status,
                "status_code": "NOT_INSPECTED" if golden_path else "GOLDEN_MISSING",
                "file_status": golden_file_status,
                "inspection_status": "NOT_INSPECTED" if golden_path else "NOT_APPLICABLE_MISSING",
                "path": str(golden_path) if golden_path else None,
                "semantics": None,
                "error": None,
            },
            "comparison": {
                "equivalence": "NOT_PROCESSED",
                "dimensions": {},
                "case_matching": {
                    "status": "NOT_COMPARED",
                    "reason": "function was not returned by the generation run",
                },
                "gaps": [missing_gap],
            },
        })

    if index_error:
        index_gap = _gap(
            function="<project-index>", category="RUN_INCOMPLETE",
            owner_layer=_CATEGORY_OWNERS["RUN_INCOMPLETE"],
            dimension="index",
            detail=f"索引身份无法读取: {index_error}",
            evidence={"index_csv": str(manifest.index_csv)},
        )
        index_gap["root_cause_status"] = "CANDIDATE_ROOT"
        all_gaps.append(index_gap)

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
    root_gaps = [
        item for item in all_gaps
        if item.get("root_cause_status") not in {"DERIVED", "REPORTING_LIMIT"}
    ]
    derived_gaps = [
        item for item in all_gaps
        if item.get("root_cause_status") == "DERIVED"
    ]
    reporting_limit_gaps = [
        item for item in all_gaps
        if item.get("root_cause_status") == "REPORTING_LIMIT"
    ]
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
    case_match_unknown_functions = 0
    for item in function_reports:
        matching = item["comparison"].get("case_matching") or {}
        counts = matching.get("counts")
        if isinstance(counts, dict):
            case_match_counts.update(counts)
        elif matching.get("status") in {
                "NOT_COMPARED", "SKIPPED_GENERATION_GATE", "MATCHING_BUDGET_EXCEEDED"}:
            case_match_unknown_functions += 1
    case_match_counts = dict(sorted(case_match_counts.items()))
    golden_file_statuses = [
        item["golden"].get("file_status", item["golden"].get("availability"))
        for item in function_reports
    ]
    golden_inspection_statuses = [
        item["golden"].get("inspection_status", item["golden"].get("status"))
        for item in function_reports
    ]
    golden_valid = sum(
        status == "PARSED" for status in golden_inspection_statuses
    )
    golden_available = sum(status == "PRESENT" for status in golden_file_statuses)
    golden_missing = sum(status == "MISSING" for status in golden_file_statuses)
    golden_invalid = sum(
        status == "PARSE_FAILED" for status in golden_inspection_statuses
    )
    golden_not_inspected = sum(
        file_status == "PRESENT" and inspection != "PARSED"
        for file_status, inspection in zip(
            golden_file_statuses, golden_inspection_statuses
        )
    )

    def _known_sum(value_getter) -> tuple[int | None, int]:
        known = [value for value in (value_getter(item) for item in function_reports)
                 if isinstance(value, int) and not isinstance(value, bool)]
        return (sum(known) if known else None, len(function_reports) - len(known))

    golden_testcases, golden_testcases_unknown = _known_sum(
        lambda item: (item["golden"]["semantics"] or {}).get("testcase_count")
    )
    generated_intents, generated_intents_unknown = _known_sum(
        lambda item: (item["generated"]["semantics"] or {}).get("intent_count")
    )
    generated_testcases, generated_testcases_unknown = _known_sum(
        lambda item: (item["generated"]["csv_semantics"] or {}).get("testcase_count")
    )
    indexed_function_count = len(expected_records) or len(function_reports)
    missing_functions = sum(
        item["generated"]["status"] in {"NOT_PROCESSED", "BLOCKED"}
        for item in function_reports
    )
    failed_functions = sum(
        item["generated"]["status"] in {"FAILED", "INVALID"}
        for item in function_reports
    )
    skipped_functions = sum(
        item["generated"]["status"] in {"SKIPPED"}
        for item in function_reports
    )
    processed_functions = len(function_reports) - missing_functions
    complete_functions = processed_functions - failed_functions - skipped_functions
    return {
        "schema_version": 1,
        "report_kind": "project-validation",
        "status": (
            "PASS" if units and not all_gaps
            and len(function_reports) == indexed_function_count
            and missing_functions == 0 else "REVIEW_REQUIRED"
        ),
        "project": {
            "id": manifest.project_id,
            "baseline": getattr(context, "baseline_ref", "unknown"),
            "scope": manifest.scope,
            "function_count": indexed_function_count,
            "indexed_function_count": indexed_function_count,
        },
        "provenance": provenance,
        "extraction": extraction,
        "totals": {
            "functions": indexed_function_count,
            "indexed_functions": indexed_function_count,
            "processed_functions": processed_functions,
            "complete_functions": complete_functions,
            "failed_functions": failed_functions,
            "skipped_functions": skipped_functions,
            "missing_functions": missing_functions,
            "unaccounted_functions": max(0, indexed_function_count - len(function_reports)),
            "blocked_functions": sum(
                item["generated"]["status"] == "BLOCKED"
                for item in function_reports
            ),
            "generation_statuses": generation_statuses,
            "golden_valid": golden_valid,
            "golden_available": golden_available,
            "golden_missing": golden_missing,
            "golden_invalid": golden_invalid,
            "golden_not_inspected": golden_not_inspected,
            "golden_missing_or_invalid": golden_missing + golden_invalid,
            "equivalences": equivalences,
            "golden_testcases": golden_testcases,
            "human_testcase_count": golden_testcases,
            "golden_testcases_unknown_functions": golden_testcases_unknown,
            "generated_intents": generated_intents,
            "generated_intents_unknown_functions": generated_intents_unknown,
            "generated_testcases": generated_testcases,
            "generated_testcase_count": generated_testcases,
            "generated_testcases_unknown_functions": generated_testcases_unknown,
            "case_matches": case_match_counts,
            "case_matching_unknown_functions": case_match_unknown_functions,
            "gap_count": len(all_gaps),
            "root_gap_count": len(root_gaps),
            "derived_gap_count": len(derived_gaps),
            "reporting_limit_gap_count": len(reporting_limit_gaps),
            "gap_categories": gap_categories,
            "unclassified_mismatches": 0,
        },
        "taxonomy": {
            "categories": list(STANDARD_GAP_CATEGORIES),
            "calibration_classifications": list(CALIBRATION_CLASSIFICATIONS),
            "owner_layers": dict(_CATEGORY_OWNERS),
            "dimension_owners": dict(_DIMENSION_OWNERS),
            "review_policy": (
                "count direction never selects a root cause; uncertain case "
                "evidence remains a review gap for wan37"
            ),
        },
        "calibration": {
            "status": "NOT_REQUIRED" if not all_gaps else "NEEDS_REVIEW",
            "golden_role": "detector_only",
            "source_evidence": {
                "baseline_document": str(manifest.baseline_document),
                "baseline_sheet": manifest.baseline_sheet,
                "baseline_revision": manifest.baseline_revision,
            },
            "investigation_order": [
                {"layer": layer, "classification_if_first_divergence": result}
                for layer, result in _CALIBRATION_INVESTIGATION_ORDER
            ],
            "cross_project_evidence": {
                "status": "NOT_ASSESSED",
                "required_for_rule_promotion": True,
            },
            "policy": (
                "observed gaps are not calibration conclusions; inspect "
                "upstream evidence in the declared order before changing "
                "docs, runtime, implementation, or rules"
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
    project_id = str(report.get("project", {}).get("id", "unknown"))
    progress = ProgressRecorder(
        path.parent / "progress-events.jsonl",
        project=project_id,
        run_id=path.parent.name,
    )
    with progress.stage(
        "__project__", "report_serialization_write", {"report": path.name}
    ) as timing:
        report_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        path.write_text(report_text, encoding="utf-8", newline="\n")
        timing["metadata"].update({"report_bytes": path.stat().st_size})
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
        f"| Complete functions | {totals.get('complete_functions', 0)} |",
        f"| Failed functions | {totals.get('failed_functions', 0)} |",
        f"| Missing/unprocessed functions | {totals.get('missing_functions', 0)} |",
        f"| Golden testcases | {totals.get('human_testcase_count', 0)} |",
        f"| Generated testcases | {totals.get('generated_testcase_count', 0)} |",
        f"| Golden files present | {totals.get('golden_available', 0)} |",
        f"| Golden files parsed | {totals.get('golden_valid', 0)} |",
        f"| Golden files not inspected | {totals.get('golden_not_inspected', 0)} |",
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
    project_id = str(report.get("project", {}).get("id", "unknown"))
    progress = ProgressRecorder(
        path.parent / "progress-events.jsonl",
        project=project_id,
        run_id=path.parent.name,
    )
    with progress.stage(
        "__project__", "report_serialization_write", {"report": path.name}
    ) as timing:
        path.write_text(
            render_project_validation_markdown(report),
            encoding="utf-8", newline="\n",
        )
        timing["metadata"].update({"report_bytes": path.stat().st_size})
    return path


__all__ = [
    "CALIBRATION_CLASSIFICATIONS", "ProjectCorpusManifest", "STANDARD_GAP_CATEGORIES",
    "build_corpus_validation_report", "compare_function_semantics",
    "golden_for_unit", "load_corpus_manifest", "normalize_generated_manifest",
    "preflight_corpus",
    "validate_corpus_paths", "write_corpus_validation_report",
    "render_project_validation_markdown", "write_project_validation_markdown",
]
