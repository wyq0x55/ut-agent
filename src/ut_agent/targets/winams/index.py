"""CSV-driven index generation using one C++ Clang extraction pass.

The project index is only a target manifest.  It is never used as a CSV
template: source AST facts are extracted first, and the original WinAMS
TestCsv is read only by the optional comment comparison step.
"""
from __future__ import annotations

import csv
import json
import re
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
    source_root = product_root / "src"
    if not source_root.is_dir():
        source_root = product_root
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
    targets = tuple((row.source_path, row.function) for row in rows)
    extracted = extractor.extract_targets(context, targets, cwd=source_root)
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
        if project_context is not None:
            suite = generate_suite(ir, project_context)
            generation_document = suite.to_dict()
            generation_status = suite.status
            _write_cp932(
                testcsv,
                csv_render.render_suite_csv(
                    ir, suite,
                    source_label=f"{row.source_name}/{row.function}",
                    title=f"{row.function} 単体テスト",
                ),
            )
            csv_intent_count = sum(
                item.validation.valid for item in suite.intents
            )
        else:
            generation = generate_intents(ir, rule_pack)
            generation_document = generation.to_dict()
            generation_status = generation.status
            _write_cp932(
                testcsv,
                csv_render.render_intents_csv(
                    ir,
                    generation,
                    source_label=f"{row.source_name}/{row.function}",
                    title=f"{row.function} 単体テスト",
                ),
            )
            csv_intent_count = len(generation.validated_intents)
        generation_document.update({
            "csv_written": True,
            "csv_kind": (
                "validated" if generation_status == "VALIDATED"
                else "partial_candidate"
            ),
            "csv_intent_count": csv_intent_count,
        })
        stub.parent.mkdir(parents=True, exist_ok=True)
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
        define_var = output_dir / "DefineVar.dat"
        _write_cp932(define_var, render_define_var(entries_from_ir(ir)))
        (output_dir / "WinAMS.INI").write_text(
            render_winams_ini(define_var), encoding="utf-8", newline="\n"
        )
        units.append(GeneratedIndexUnit(
            row=row,
            output_dir=output_dir,
            testcsv=testcsv,
            stub=stub,
            ir_json=ir_json,
            intent_manifest=intent_manifest,
            status=generation_status,
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
        "extraction": {
            "mode": "one_cpp_invocation_targets_file",
            "source_files": len(context_sources),
            "targets": len(rows),
        },
        "units": len(units),
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
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "index-generation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return tuple(units)


__all__ = [
    "GeneratedIndexUnit",
    "IndexRow",
    "compare_comment_rows",
    "generate_project_from_index",
    "load_index",
]
