"""CSV-driven index generation using one C++ Clang extraction pass.

The project index is only a target manifest.  It is never used as a CSV
template: source AST facts are extracted first, and the original WinAMS
TestCsv is read only by the optional comment comparison step.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from ut_agent.toolchain import (
    ClangExtractor,
    default_clang_extractor,
    discover_compile_sources,
    make_compile_context,
)
from ut_agent.generation import generate_intents, generate_suite, load_rule_pack
from ut_agent.observability import ProgressRecorder
from ut_agent.targets.winams import stub as stub_generate
from ut_agent.targets.winams import csv as csv_render
from ut_agent.targets.winams.define_var import (
    entries_from_ir,
    render_define_var,
    render_winams_ini,
)

if TYPE_CHECKING:
    from ut_agent.project.model import ResolvedProjectContext


@dataclass(frozen=True)
class IndexRow:
    row_number: int
    callcnt: int
    source_name: str
    function: str
    source_path: Path
    target_rel: Path
    target_base_rel: Path


@dataclass(frozen=True)
class GeneratedIndexUnit:
    row: IndexRow
    output_dir: Path
    testcsv: Path
    stub: Path
    ir_json: Path
    intent_manifest: Path
    status: str
    error: str | None = None


def _intent_summary(document: dict[str, object]) -> dict[str, object]:
    """Return small generation metadata for downstream validation routing.

    The full intent document can contain large, evidence-bearing table state.
    This summary is emitted from that already-created document and never
    replaces it; callers may use it to decide whether detailed semantic
    matching is within the declared cardinality budget.
    """
    intents = [item for item in document.get("intents", []) if isinstance(item, dict)]
    obligations = [
        item.get("obligation", {}) for item in intents
        if isinstance(item.get("obligation", {}), dict)
    ]
    validations = [
        item.get("validation", {}) for item in intents
        if isinstance(item.get("validation", {}), dict)
    ]
    solves = [
        item for item in document.get("solve_results", []) if isinstance(item, dict)
    ]
    evaluations = [
        item for item in document.get("evaluations", []) if isinstance(item, dict)
    ]
    return {
        "schema_version": 1,
        "status": str(document.get("status", "UNKNOWN")),
        "csv_written": bool(document.get("csv_written", False)),
        "csv_kind": str(document.get("csv_kind", "not_written")),
        "csv_intent_count": document.get("csv_intent_count"),
        "intent_count": len(intents),
        "obligation_count": len(obligations),
        "validated_intent_count": sum(
            item.get("status") == "VALIDATED" and not item.get("errors")
            for item in validations
        ),
        "obligation_kinds": dict(sorted(Counter(
            str(item.get("kind", "unknown")) for item in obligations
        ).items())),
        "outcomes": dict(sorted(Counter(
            "TRUE" if item.get("outcome") is True else
            "FALSE" if item.get("outcome") is False else "UNSPECIFIED"
            for item in obligations
        ).items())),
        "boundary_classes": dict(sorted(Counter(
            str(item.get("boundary_class")) for item in obligations
            if item.get("boundary_class") is not None
        ).items())),
        "pair_count": len({
            item.get("pair_id") for item in obligations if item.get("pair_id")
        }),
        "solve_statuses": dict(sorted(Counter(
            str(item.get("status", "unknown")) for item in solves
        ).items())),
        "evaluation_count": len(evaluations),
        "evaluation_complete_count": sum(bool(item.get("complete")) for item in evaluations),
        "evaluation_statuses": dict(sorted(Counter(
            str(item.get("status", "unknown")) for item in evaluations
        ).items())),
        "validation_statuses": dict(sorted(Counter(
            str(item.get("status", "unknown")) for item in validations
        ).items())),
        "input_keys": sorted({
            str(key) for item in intents
            for key in (item.get("inputs", {}) or {})
        }),
        "expected_keys": sorted({
            str(key) for item in intents
            for key in (item.get("expected", {}) or {})
        }),
        "stub_keys": sorted({
            str(key) for item in intents
            for key in (item.get("stub_behavior", {}) or {})
        }),
        "issues": sorted(str(item) for item in (document.get("issues", []) or [])),
        "details_status": "SUMMARY_ONLY",
    }


def _path_after_marker(value: str, marker: Sequence[str]) -> Path:
    parts = [item for item in re.split(r"[\\/]", value.strip()) if item]
    lowered = [item.lower() for item in parts]
    wanted = [item.lower() for item in marker]
    for index in range(len(parts) - len(wanted) + 1):
        if lowered[index:index + len(wanted)] == wanted:
            tail = parts[index + len(wanted):]
            if tail:
                return Path(*tail)
    raise ValueError(f"路径不包含 {'/'.join(marker)}：{value}")


def load_index(index_csv: Path, product_root: Path) -> tuple[IndexRow, ...]:
    """Load the five-column project index and bind each row to local Soft."""
    index_csv = Path(index_csv).resolve()
    product_root = Path(product_root).resolve()
    rows: list[IndexRow] = []
    seen: set[tuple[Path, str]] = set()
    with index_csv.open("r", encoding="cp932", newline="") as stream:
        for row_number, values in enumerate(csv.reader(stream), start=1):
            if not values or all(not value.strip() for value in values):
                continue
            if len(values) < 5:
                raise ValueError(f"索引 CSV 第 {row_number} 行少于 5 列")
            try:
                callcnt = int(values[0].strip())
            except ValueError as error:
                raise ValueError(
                    f"索引 CSV 第 {row_number} 行 callcnt 无效：{values[0]}"
                ) from error
            source_name = values[1].strip()
            function = values[2].strip()
            try:
                source_rel = _path_after_marker(values[3], ("Product", "src"))
                source_path = (product_root / "src" / source_rel).resolve()
            except ValueError:
                # Different SVN packages name the product checkout either
                # ``Product`` or ``Soft``.  Soft packages may add a product
                # layer such as ``Soft/00_General/src``; retain that complete
                # path below the supplied Soft root.
                source_rel = _path_after_marker(values[3], ("Soft",))
                source_path = (product_root / source_rel).resolve()
            try:
                target_rel = _path_after_marker(values[4], ("WinAMS", "src"))
                target_base_rel = Path("WinAMS") / "src"
            except ValueError:
                # Soft packages mirror their target under winAMS/<product>/src
                # instead of the Product-package WinAMS/src layout.
                target_rel = _path_after_marker(values[4], ("winAMS",))
                target_base_rel = Path("winAMS")
            if source_rel.name.lower() != source_name.lower():
                raise ValueError(
                    f"索引 CSV 第 {row_number} 行源文件名不一致："
                    f"{source_name} != {source_rel.name}"
                )
            if not source_path.is_file():
                raise FileNotFoundError(
                    f"索引 CSV 第 {row_number} 行源码不存在：{source_path}"
                )
            key = (source_path, function)
            if key in seen:
                raise ValueError(f"索引 CSV 第 {row_number} 行目标重复：{source_path}:{function}")
            seen.add(key)
            rows.append(IndexRow(
                row_number=row_number,
                callcnt=callcnt,
                source_name=source_name,
                function=function,
                source_path=source_path,
                target_rel=target_rel,
                target_base_rel=target_base_rel,
            ))
    if not rows:
        raise ValueError(f"索引 CSV 没有目标：{index_csv}")
    return tuple(rows)


def load_generated_project_units(
    index_csv: Path, product_root: Path, output_root: Path,
) -> tuple[GeneratedIndexUnit, ...]:
    """Reuse only a completed, identity-bearing generation output.

    This is deliberately a read-only bridge from generation artifacts to the
    reporting layer. A missing completion report, missing artifact, or stale
    artifact hash is a hard diagnostic failure; callers must not turn such an
    output into a formal corpus-validation result.
    """
    index_csv = Path(index_csv).resolve()
    product_root = Path(product_root).resolve()
    output_root = Path(output_root).resolve()
    report_path = output_root / "index-generation-report.json"
    if not report_path.is_file():
        raise ValueError(f"复用产物缺少 index-generation-report.json: {output_root}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("run_status") != "COMPLETED":
        raise ValueError(f"复用产物未完成: {report.get('run_status')}")
    if Path(str(report.get("index_csv", ""))).resolve() != index_csv:
        raise ValueError("复用产物 index_csv 身份不一致")
    if Path(str(report.get("product_root", ""))).resolve() != product_root:
        raise ValueError("复用产物 product_root 身份不一致")
    rows = load_index(index_csv, product_root)
    if int(report.get("expected_units", -1)) != len(rows):
        raise ValueError("复用产物 expected_units 与当前索引不一致")

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    units: list[GeneratedIndexUnit] = []
    for row in rows:
        output_dir = output_root / row.target_rel
        testcsv = output_dir / "TestCsv" / f"{row.function}.csv"
        stub = output_dir / "AMSTB_SrcFile.c"
        ir_json = output_dir / "function-ir.json"
        intent_manifest = output_dir / "test-intents.json"
        summary_path = output_dir / "test-intents-summary.json"
        required = (testcsv, stub, ir_json, intent_manifest, summary_path)
        if any(not path.is_file() for path in required):
            raise ValueError(f"复用产物不完整: row={row.row_number} {row.function}")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        identity = summary.get("artifact_identity")
        if not isinstance(identity, dict):
            raise ValueError(f"复用摘要缺少 artifact_identity: {summary_path}")
        expected_hashes = {
            "testcsv": testcsv, "stub": stub, "function_ir": ir_json,
            "intent_manifest": intent_manifest,
        }
        for key, path in expected_hashes.items():
            if identity.get(key) != digest(path):
                raise ValueError(f"复用产物哈希不一致: {path}")
        units.append(GeneratedIndexUnit(
            row=row, output_dir=output_dir, testcsv=testcsv, stub=stub,
            ir_json=ir_json, intent_manifest=intent_manifest,
            status=str(summary.get("status", "UNKNOWN")),
        ))
    return tuple(units)


def _direct_includes(source: Path) -> tuple[str, ...]:
    text = source.read_bytes().decode("cp932", errors="replace")
    found = re.findall(r'^\s*#\s*include\s*[<"]([^">]+)[">]', text, re.MULTILINE)
    return tuple(dict.fromkeys(found))


def _write_cp932(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        text.replace("\r\n", "\n").replace("\r", "\n")
        .replace("\n", "\r\n").encode("cp932")
    )


def _comment_row(path: Path) -> list[str] | None:
    with path.open("r", encoding="cp932", errors="replace", newline="") as stream:
        for row in csv.reader(stream):
            if row and row[0] == "#COMMENT":
                return row
    return None


def compare_comment_rows(actual: Path, expected: Path) -> dict[str, object]:
    """Compare only the WinAMS ``#COMMENT`` row; golden never enters render."""
    actual_row = _comment_row(actual) if actual.is_file() else None
    expected_row = _comment_row(expected) if expected.is_file() else None
    equal = actual_row is not None and actual_row == expected_row
    first_difference = None
    if actual_row is not None and expected_row is not None:
        for index, (left, right) in enumerate(zip(actual_row, expected_row)):
            if left != right:
                first_difference = index
                break
        if first_difference is None and len(actual_row) != len(expected_row):
            first_difference = min(len(actual_row), len(expected_row))
    return {
        "equal": equal,
        "actual_exists": actual.is_file(),
        "expected_exists": expected.is_file(),
        "actual_columns": len(actual_row or []),
        "expected_columns": len(expected_row or []),
        "first_difference": first_difference,
        "actual": actual_row,
        "expected": expected_row,
    }


def generate_project_from_index(
    index_csv: Path,
    product_root: Path,
    output_root: Path,
    *,
    reference_root: Path | None = None,
    clang_extractor: Path | None = None,
    rules_path: Path | None = None,
    defines: dict[str, str] | None = None,
    call_max: int = 5,
    extractor_timeout: float = 600.0,
    check_golden: bool = False,
    project_context: ResolvedProjectContext | None = None,
) -> tuple[GeneratedIndexUnit, ...]:
    """Generate every indexed target after one standalone C++ invocation."""
    index_csv = Path(index_csv).resolve()
    product_root = Path(product_root).resolve()
    output_root = Path(output_root).resolve()
    rows = load_index(index_csv, product_root)
    output_root.mkdir(parents=True, exist_ok=True)
    project_id = (
        project_context.project_id if project_context is not None else index_csv.stem
    )
    progress = ProgressRecorder(
        output_root / "progress-events.jsonl",
        project=project_id,
        run_id=output_root.name,
    )
    source_root = product_root / "src"
    if not source_root.is_dir():
        source_root = product_root
    targets = tuple((row.source_path, row.function) for row in rows)
    try:
        with progress.stage(
            "__project__", "extraction", {"target_count": len(targets)}
        ) as timing:
            context_sources = discover_compile_sources(source_root)
            include_dirs = tuple(
                sorted(
                    {product_root, *(item for item in product_root.rglob("*") if item.is_dir())},
                    key=lambda item: item.as_posix().lower(),
                )
            )
            context = make_compile_context(context_sources, include_dirs, defines or {})
            extractor = ClangExtractor(
                Path(clang_extractor).resolve() if clang_extractor else default_clang_extractor(),
                timeout=extractor_timeout,
            )
            extracted = extractor.extract_targets(context, targets, cwd=source_root)
            timing["metadata"].update({
                "source_file_count": len(context_sources),
                "include_dir_count": len(include_dirs),
                "extracted_function_count": len(extracted),
            })
    except Exception as exc:
        report = {
            "index_csv": str(index_csv),
            "product_root": str(product_root),
            "output_root": str(output_root),
            "run_status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "extraction": {
                "mode": "one_cpp_invocation_targets_file",
                "target_count": len(targets),
            },
            "units": 0,
            "expected_units": len(rows),
            "statuses": {"NOT_PROCESSED": len(rows)},
        }
        with progress.stage(
            "__project__", "report_serialization_write",
            {"report": "index-generation-report.json"},
        ):
            (output_root / "index-generation-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        return ()
    rule_pack = load_rule_pack(Path(rules_path).resolve() if rules_path else None)
    units: list[GeneratedIndexUnit] = []
    comparisons: list[dict[str, object]] = []
    reference_base = Path(reference_root).resolve() if reference_root else index_csv.parent.parent

    for row in rows:
        ir = extracted[(row.source_path, row.function)]
        output_dir = output_root / row.target_rel
        testcsv = output_dir / "TestCsv" / f"{row.function}.csv"
        stub = output_dir / "AMSTB_SrcFile.c"
        ir_json = output_dir / "function-ir.json"
        intent_manifest = output_dir / "test-intents.json"
        intent_summary = output_dir / "test-intents-summary.json"
        generation_status = "FAILED"
        generation_document: dict[str, object] | None = None
        csv_text = ""
        csv_intent_count: int | None = None
        error: str | None = None
        try:
            with progress.stage(
                row.function,
                "single_function_generation",
                {"row": row.row_number, "source": str(row.source_path)},
            ) as timing:
                if project_context is not None:
                    suite = generate_suite(ir, project_context)
                    generation_status = suite.status
                    generated_intents = suite.intents
                    validated_intents = suite.validated_intents
                    issues = suite.issues
                else:
                    generation = generate_intents(ir, rule_pack)
                    generation_status = generation.status
                    generated_intents = generation.intents
                    validated_intents = generation.validated_intents
                    issues = generation.issues
                csv_intent_count = len(validated_intents)
                timing["status"] = generation_status
                timing["metadata"].update({
                    "obligation_count": len(generated_intents),
                    "intent_count": len(generated_intents),
                    "validated_intent_count": csv_intent_count,
                    "issues": list(issues),
                })
            with progress.stage(
                row.function, "suite_conversion", {"row": row.row_number}
            ) as timing:
                generation_document = (
                    suite.to_dict() if project_context is not None
                    else generation.to_dict()
                )
                timing["metadata"].update({
                    "intent_count": len(generation_document.get("intents", [])),
                    "solve_count": len(generation_document.get("solve_results", [])),
                    "evaluation_count": len(generation_document.get("evaluations", [])),
                })
            generation_document.update({
                "csv_written": True,
                "csv_kind": (
                    "validated" if generation_status == "VALIDATED"
                    else "partial_candidate"
                ),
                "csv_intent_count": csv_intent_count,
            })
            with progress.stage(
                row.function, "csv_projection", {"row": row.row_number}
            ) as timing:
                if project_context is not None:
                    csv_text = csv_render.render_suite_csv(
                        ir, suite,
                        source_label=f"{row.source_name}/{row.function}",
                        title=f"{row.function} 単体テスト",
                    )
                else:
                    csv_text = csv_render.render_intents_csv(
                        ir,
                        generation,
                        source_label=f"{row.source_name}/{row.function}",
                        title=f"{row.function} 単体テスト",
                    )
                timing["metadata"].update({
                    "csv_row_count": csv_text.count("\n"),
                    "csv_char_count": len(csv_text),
                })
            with progress.stage(
                row.function, "artifact_serialization_write", {"row": row.row_number}
            ) as timing:
                stub.parent.mkdir(parents=True, exist_ok=True)
                _write_cp932(testcsv, csv_text)
                stub.write_text(
                    stub_generate.render_stub_c(
                        ir, call_max, extra_includes=_direct_includes(row.source_path)
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                ir_json.write_text(
                    json.dumps(ir.to_dict(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                intent_manifest.write_text(
                    json.dumps(generation_document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                artifact_identity = {
                    "testcsv": hashlib.sha256(testcsv.read_bytes()).hexdigest(),
                    "stub": hashlib.sha256(stub.read_bytes()).hexdigest(),
                    "function_ir": hashlib.sha256(ir_json.read_bytes()).hexdigest(),
                    "intent_manifest": hashlib.sha256(intent_manifest.read_bytes()).hexdigest(),
                }
                summary = _intent_summary(generation_document)
                summary["artifact_identity"] = artifact_identity
                intent_summary.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                define_var = output_dir / "DefineVar.dat"
                _write_cp932(define_var, render_define_var(entries_from_ir(ir)))
                (output_dir / "WinAMS.INI").write_text(
                    render_winams_ini(define_var), encoding="utf-8", newline="\n"
                )
                timing["metadata"].update({
                    "artifact_bytes": {
                        "testcsv": testcsv.stat().st_size,
                        "function_ir": ir_json.stat().st_size,
                        "intent_manifest": intent_manifest.stat().st_size,
                        "intent_summary": intent_summary.stat().st_size,
                    },
                })
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        units.append(GeneratedIndexUnit(
            row=row,
            output_dir=output_dir,
            testcsv=testcsv,
            stub=stub,
            ir_json=ir_json,
            intent_manifest=intent_manifest,
            status=generation_status,
            error=error,
        ))
        if check_golden:
            expected = (
                reference_base / row.target_base_rel / row.target_rel
                / "TestCsv" / f"{row.function}.csv"
            )
            item = compare_comment_rows(testcsv, expected)
            item.update({
                "row": row.row_number,
                "function": row.function,
                "actual_path": str(testcsv),
                "expected_path": str(expected),
            })
            comparisons.append(item)

    report = {
        "index_csv": str(index_csv),
        "product_root": str(product_root),
        "output_root": str(output_root),
        "run_status": "COMPLETED",
        "extraction": {
            "mode": "one_cpp_invocation_targets_file",
            "source_files": len(context_sources),
            "targets": len(rows),
        },
        "units": len(units),
        "expected_units": len(rows),
        "statuses": {
            status: sum(unit.status == status for unit in units)
            for status in sorted({unit.status for unit in units})
        },
    }
    if check_golden:
        report["comment_comparison"] = {
            "total": len(comparisons),
            "equal": sum(bool(item["equal"]) for item in comparisons),
            "different": sum(not bool(item["equal"]) for item in comparisons),
            "missing_actual": sum(not bool(item["actual_exists"]) for item in comparisons),
            "missing_expected": sum(not bool(item["expected_exists"]) for item in comparisons),
            "items": comparisons,
        }
    with progress.stage(
        "__project__", "report_serialization_write",
        {"report": "index-generation-report.json"},
    ) as timing:
        report_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        (output_root / "index-generation-report.json").write_text(
            report_text,
            encoding="utf-8",
            newline="\n",
        )
        timing["metadata"].update({
            "report_bytes": (output_root / "index-generation-report.json").stat().st_size,
            "unit_count": len(units),
        })
    return tuple(units)


__all__ = [
    "GeneratedIndexUnit",
    "IndexRow",
    "compare_comment_rows",
    "generate_project_from_index",
    "load_index",
]
