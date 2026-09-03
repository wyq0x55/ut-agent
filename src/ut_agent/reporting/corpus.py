"""Deterministic project-corpus validation and semantic gap reporting.

This module is deliberately downstream of generation.  It reads the explicit
project index, generated FunctionIR/test-intent/CSV artifacts, and reviewed
Golden CSVs; it never supplies Golden data to the generator or oracle.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ut_agent.learning.golden import normalize_golden_csv, semantic_csv_signature


STANDARD_GAP_CATEGORIES = (
    "BASELINE_GAP",
    "BASELINE_CONVERSION_MISS",
    "BASELINE_IMPLICIT_REQUIREMENT",
    "STABLE_HUMAN_CONVENTION",
    "PROJECT_SPECIFIC_ADDITION",
    "FUNCTION_IR_GAP",
    "OBLIGATION_GAP",
    "SOLVER_GAP",
    "EVALUATOR_GAP",
    "ORACLE_GAP",
    "VALIDATION_GAP",
    "PROJECTION_GAP",
    "HARNESS_GAP",
    "GOLDEN_ERROR",
    "GENERATOR_BUG",
)

_DIMENSION_OWNERS = {
    "testcase_count": "baseline/generation",
    "viewpoint": "baseline/rules",
    "condition_combination": "baseline/mcdc-rules",
    "boundary_domain": "baseline/rules",
    "stub": "stub/target-adapter",
    "oracle": "oracle",
    "required_values": "baseline/oracle",
    "projection": "winams-projection",
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
    if "validation" in text or status in {"NEEDS_REVIEW", "INVALID"}:
        return "VALIDATION_GAP"
    return "GENERATOR_BUG"


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


def compare_function_semantics(
    *,
    function: str,
    generated_manifest: dict[str, Any],
    generated_csv: dict[str, Any] | None,
    golden: dict[str, Any] | None,
    actual_csv_path: Path,
    golden_csv_path: Path | None,
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
    if generated_manifest.get("status") != "VALIDATED":
        category = _generation_gap_category(generated_manifest)
        dimensions = _comparison_dimensions(generated_manifest, generated_csv, golden)
        return {
            "equivalence": "GENERATION_GATE_FAILED",
            "dimensions": dimensions,
            "gaps": [_gap(
                function=function, category=category,
                owner_layer="generation", dimension="generation_status",
                detail="生成结果未通过 VALIDATED 门禁",
                evidence={
                    "status": generated_manifest.get("status"),
                    "issues": generated_manifest.get("issues", []),
                },
            )],
        }
    if generated_csv is None:
        dimensions = _comparison_dimensions(generated_manifest, generated_csv, golden)
        return {
            "equivalence": "GENERATED_CSV_MISSING",
            "dimensions": dimensions,
            "gaps": [_gap(
                function=function, category="GENERATOR_BUG",
                owner_layer="winams-projection", dimension="projection",
                detail="生成 manifest 已 VALIDATED 但 TestCsv 不存在或无法解析",
                evidence={"actual_path": str(actual_csv_path)},
            )],
        }

    dimensions = _comparison_dimensions(generated_manifest, generated_csv, golden)
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
        elif status == "missing_generated":
            category = "BASELINE_GAP"
        elif status == "extra_generated":
            category = "BASELINE_CONVERSION_MISS"
        else:
            category = "BASELINE_IMPLICIT_REQUIREMENT"
        gaps.append(_gap(
            function=function, category=category,
            owner_layer=_DIMENSION_OWNERS[name], dimension=name,
            detail=f"{name} semantic dimension differs ({status})",
            evidence={"generated": item["generated"], "golden": item["golden"]},
        ))
    return {"equivalence": equivalence, "dimensions": dimensions, "gaps": gaps}


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


def build_corpus_validation_report(
    manifest: ProjectCorpusManifest,
    context: Any,
    units: tuple[Any, ...],
    *,
    output_root: Path,
    generator_commit: str = "unknown",
    generator_version: str = "0.1.0",
) -> dict[str, Any]:
    """Build a machine-readable all-functions generation/compare report."""
    function_reports: list[dict[str, Any]] = []
    all_gaps: list[dict[str, Any]] = []
    for unit in units:
        golden_path = golden_for_unit(manifest, unit)
        golden = None
        golden_error = None
        if golden_path is not None:
            golden, golden_error = _safe_normalize(golden_path)
        generated_manifest = normalize_generated_manifest(unit.intent_manifest)
        actual_csv = None
        actual_error = None
        if unit.testcsv.is_file():
            actual_csv, actual_error = _safe_normalize(unit.testcsv)
        comparison = compare_function_semantics(
            function=unit.row.function,
            generated_manifest=generated_manifest,
            generated_csv=actual_csv,
            golden=golden,
            actual_csv_path=unit.testcsv,
            golden_csv_path=golden_path,
        )
        if golden_error:
            comparison["gaps"] = [_gap(
                function=unit.row.function, category="GOLDEN_ERROR",
                owner_layer="golden", dimension="golden_file",
                detail=f"Golden 解析失败: {golden_error}",
                evidence={"golden_path": str(golden_path)},
            )]
            comparison["equivalence"] = "GOLDEN_INVALID"
        if actual_error and generated_manifest.get("status") == "VALIDATED":
            comparison["gaps"].append(_gap(
                function=unit.row.function, category="GENERATOR_BUG",
                owner_layer="winams-projection", dimension="projection",
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
                "INVALID" if golden_error else "MISSING",
                "path": str(golden_path) if golden_path else None,
                "semantics": golden,
                "error": golden_error,
            },
            "comparison": comparison,
        })

    generation_statuses = _counts([str(unit.status) for unit in units])
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
        "generated": item["generated"]["semantics"],
        "generated_csv": item["generated"]["csv_semantics"],
        "golden": item["golden"]["semantics"],
        "equivalence": item["comparison"]["equivalence"],
        "dimensions": item["comparison"]["dimensions"],
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
    return {
        "schema_version": 1,
        "status": "PASS" if units and not all_gaps else "REVIEW_REQUIRED",
        "project": {
            "id": manifest.project_id,
            "baseline": getattr(context, "baseline_ref", "unknown"),
            "scope": manifest.scope,
            "function_count": len(units),
        },
        "provenance": provenance,
        "extraction": extraction,
        "totals": {
            "functions": len(units),
            "generation_statuses": generation_statuses,
            "golden_valid": sum(item["golden"]["status"] == "VALID" for item in function_reports),
            "golden_missing_or_invalid": sum(
                item["golden"]["status"] != "VALID" for item in function_reports
            ),
            "equivalences": equivalences,
            "golden_testcases": sum(
                int((item["golden"]["semantics"] or {}).get("testcase_count", 0))
                for item in function_reports
            ),
            "generated_intents": sum(
                int(item["generated"]["semantics"].get("intent_count", 0))
                for item in function_reports
            ),
            "generated_testcases": sum(
                int((item["generated"]["csv_semantics"] or {}).get("testcase_count", 0))
                for item in function_reports
            ),
            "gap_count": len(all_gaps),
            "gap_categories": gap_categories,
            "unclassified_mismatches": 0,
        },
        "taxonomy": {
            "categories": list(STANDARD_GAP_CATEGORIES),
            "owner_layers": dict(_DIMENSION_OWNERS),
            "review_policy": "uncertain differences remain gaps for wan37 triage",
        },
        "functions": function_reports,
        "stability": {
            "semantic_fingerprint": _digest(stable_functions),
            "projection_fingerprint": _digest(projection_stable),
            "inputs_excluded_from_fingerprint": [
                "output paths", "generator commit", "Golden row order"
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


__all__ = [
    "ProjectCorpusManifest", "STANDARD_GAP_CATEGORIES",
    "build_corpus_validation_report", "compare_function_semantics",
    "golden_for_unit", "load_corpus_manifest", "normalize_generated_manifest",
    "validate_corpus_paths", "write_corpus_validation_report",
]
