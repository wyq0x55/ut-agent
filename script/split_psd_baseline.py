#!/usr/bin/env python3
"""Split the PSD再構築 workbook sheet into reviewable test-baseline sections.

The section boundaries are explicit source-row coordinates from Ver.1.6.  The
script does not infer rules from prose: it only copies populated cells into
per-baseline YAML/Markdown artifacts and records excluded, out-of-scope
material in the manifest.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from convert_baseline_xlsx import _markdown_value, _yaml_lines, convert


SHEET = "PSD再構築"
REVISION = "Ver.1.6"
BASELINE_ID = "psd-rebuild-mcdc"


SECTION_SPECS = (
    ("0-1", "0-1-evaluation", (29, 34), 31, "评估观点", "验证门禁", "validation_gate", False,
     "覆盖率、期待值和边界安全性是验收门禁，不直接生成输入。"),
    ("0-2", "0-2-typed-domain", (37, 54), 37, "各类型值的分类", "生成输入基准", "generation_input", True,
     "按类型复制最小值、中间值、最大值；具体宽度以 FunctionIR 类型事实为准。"),
    ("1-1", "1-1-variable-assignment", (56, 65), 58, "左右两边都是变量时的赋值", "生成输入基准", "generation_input", True,
     "变量到变量赋值后确认目标变量状态。"),
    ("1-2", "1-2-constant-assignment", (67, 73), 67, "右边是常量时的赋值", "生成输入基准", "generation_input", True,
     "常量赋值前设置与目标值不同的初始状态。"),
    ("1-3", "1-3-array-assignment", (75, 82), 75, "数组赋值", "生成输入基准", "generation_input", True,
     "固定数组索引，确认目标元素变化且其他元素保持语义不变。"),
    ("1-4", "1-4-register-io-assignment", (84, 90), 84, "寄存器、I/O 端口的值设置", "生成输入基准", "generation_input", True,
     "使用设计书地址、访问宽度和尺寸设置寄存器/I-O。"),
    ("3-1", "3-1-call-count", (95, 111), 97, "函数调用", "生成输入基准", "generation_input", True,
     "确认外部函数调用次数和 Stub 内调用计数。"),
    ("3-2", "3-2-return-value", (113, 119), 113, "函数返回值", "生成输入基准", "generation_input", True,
     "将 Stub 返回值作为可观察输入，并确认目标函数使用后的结果。"),
    ("3-3", "3-3-arguments", (121, 127), 121, "函数参数", "生成输入基准", "generation_input", True,
     "将 Stub 实参作为可观察输入；指针方向必须有 AST 证据。"),
    ("4-1", "4-1-variable-compare", (130, 142), 132, "变量之间比较", "生成输入基准", "generation_input", True,
     "变量与变量比较的最小、邻接和最大值组合。"),
    ("4-2", "4-2-equality", (145, 155), 145, "变量值与常量值相等", "生成输入基准", "generation_input", True,
     "变量与常量相等比较的常量邻接值、常量值、类型最小/最大值。"),
    ("4-3", "4-3-relational", (158, 177), 158, "常量与变量的大小比较", "生成输入基准", "generation_input", True,
     "常量与变量大小关系比较，保留使真值发生变化的邻接值。"),
    ("4-4", "4-4-mcdc", (179, 208), 179, "条件判断中包含 AND、OR", "生成输入基准", "generation_input", True,
     "AND/OR 条件组合及独立条件变化；共同变量按一个输入列处理。"),
    ("4-5", "4-5-array-compare", (210, 221), 211, "数组比较", "生成输入基准", "generation_input", True,
     "固定数组索引进行比较，其他数组元素设置为 FALSE 侧，并覆盖表数组索引。"),
    ("6-1", "6-1-control-flow", (230, 274), 231, "各种条件语句", "生成输入基准", "generation_input", True,
     "if/else-if、switch/default 和 for/while 的控制流、case 及循环次数。"),
    ("6-2", "6-2-ordering", (281, 292), 281, "测试模式排列", "确定性排序规则", "ordering_policy", True,
     "TRUE/FALSE、case 和剩余值的确定性排列。"),
)


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result or "A"


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _section_title(cells: list[dict[str, Any]], start_row: int) -> str:
    for cell in cells:
        if cell["row"] == start_row and cell["coordinate"].startswith("D"):
            return str(cell["value"])
    return ""


def _section_cells(sheet: dict[str, Any], start_row: int, end_row: int) -> list[dict[str, Any]]:
    result = [
        {
            key: cell[key]
            for key in ("coordinate", "row", "column", "cell_type", "value")
            if key in cell
        }
        for cell in sheet["cells"]
        if start_row <= cell["row"] <= end_row
    ]
    return result


def _source_range(cells: list[dict[str, Any]], start_row: int, end_row: int) -> str:
    columns = [cell["column"] for cell in cells]
    if not columns:
        return f"{SHEET}!A{start_row}:A{end_row}"
    return (
        f"{SHEET}!{_column_name(min(columns))}{start_row}:"
        f"{_column_name(max(columns))}{end_row}"
    )


def _section_payload(
    source: dict[str, Any],
    sheet: dict[str, Any],
    section_id: str,
    slug: str,
    rows: tuple[int, int],
    title_row: int,
    name_zh: str,
    category_zh: str,
    role: str,
    generation_relevant: bool,
    note: str,
    artifact_yaml: str,
    artifact_markdown: str,
) -> dict[str, Any]:
    start_row, end_row = rows
    cells = _section_cells(sheet, start_row, end_row)
    return {
        "format_version": 1,
        "kind": "baseline-section-source",
        "source": {
            "file": source["file"],
            "sheet": SHEET,
            "revision": REVISION,
            "sha256": source["sha256"],
            "semantic_status": "source_only",
            "language": "中文说明 + 日文原文证据",
            "translation_policy": "只翻译说明，不改写原始单元格内容",
        },
        "baseline": {
            "id": BASELINE_ID,
            "profile": SHEET,
            "section_id": section_id,
            "name": name_zh,
            "name_original": _section_title(sheet["cells"], title_row),
            "row_range": f"{start_row}:{end_row}",
            "source_range": _source_range(cells, start_row, end_row),
            "category": category_zh,
            "role": role,
            "role_zh": category_zh,
            "generation_relevant": generation_relevant,
            "generation_relevant_zh": "是" if generation_relevant else "否",
            "review_status": "needs_review",
            "review_status_zh": "需人工复核",
            "purpose": note,
            "mapping_note": note,
            "ai_guidance": {
                "allowed": "只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。",
                "prohibited": "不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。",
            },
            "human_review_points": [
                "确认该基准是否适用于当前产品和目标函数。",
                "确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。",
                "确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。",
            ],
            "artifact_yaml": artifact_yaml,
            "artifact_markdown": artifact_markdown,
            "cells": cells,
        },
    }


def _section_markdown(payload: dict[str, Any]) -> str:
    source = payload["source"]
    baseline = payload["baseline"]
    lines = [
        f"# PSD再構築 Ver.1.6 — {baseline['section_id']} {baseline['name']}",
        "",
        "> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。",
        "",
        "## 基准信息",
        "",
        f"- 中文基准名：**{baseline['name']}**",
        f"- 原表名称：{_markdown_value(baseline['name_original'])}",
        f"- 稳定 ID：`{baseline['section_id']}`",
        f"- 来源：`{source['file']}` / `{source['sheet']}` / `{baseline['source_range']}`",
        f"- 原文件 SHA-256：`{source['sha256']}`",
        f"- 基准类别：`{baseline['category']}`",
        f"- 是否参与生成：`{baseline['generation_relevant_zh']}`",
        f"- 复核状态：`{baseline['review_status_zh']}`",
        f"- 基准目的：{baseline['purpose']}",
        "",
        "## AI 使用边界",
        "",
        f"- 允许：{baseline['ai_guidance']['allowed']}",
        f"- 禁止：{baseline['ai_guidance']['prohibited']}",
        "",
        "## 人工复核清单",
        "",
    ]
    lines.extend(f"- {item}" for item in baseline["human_review_points"])
    lines.extend(["", "## 原始单元格证据（保留日文）"])
    current_row = None
    for cell in baseline["cells"]:
        if cell["row"] != current_row:
            current_row = cell["row"]
            lines.extend(["", f"### Row {current_row}", ""])
        lines.append(
            f"- `{cell['coordinate']}` ({cell['cell_type']}): "
            f"{_markdown_value(cell['value'])}"
        )
    return "\n".join(lines) + "\n"


def _manifest(
    source: dict[str, Any],
    sections: list[dict[str, Any]],
    excluded_sheets: list[str],
) -> dict[str, Any]:
    return {
        "format_version": 1,
        "kind": "baseline-source-manifest",
        "source": {
            "file": source["file"],
            "sheet": SHEET,
            "revision": REVISION,
            "sha256": source["sha256"],
            "semantic_status": "source_only",
            "language": "中文说明 + 日文原文证据",
            "translation_policy": "只翻译说明，不改写原始单元格内容",
            "conversion": "按测试基准显式分段；无关内容单独记录并排除",
        },
        "baseline": {
            "id": BASELINE_ID,
            "profile": SHEET,
            "name": "PSD 重构",
            "name_original": SHEET,
            "section_count": len(sections),
            "review_status": "needs_review",
            "review_status_zh": "全部需人工复核",
            "rule_status": "source_transcription_only",
            "rule_status_zh": "仅为来源转录，尚未自动批准为执行规则",
            "sections": [
                {
                    "section_id": item["baseline"]["section_id"],
                    "name": item["baseline"]["name"],
                    "name_original": item["baseline"]["name_original"],
                    "category": item["baseline"]["category"],
                    "purpose": item["baseline"]["purpose"],
                    "source_range": item["baseline"]["source_range"],
                    "role": item["baseline"]["role"],
                    "role_zh": item["baseline"]["role_zh"],
                    "generation_relevant": item["baseline"]["generation_relevant"],
                    "generation_relevant_zh": item["baseline"]["generation_relevant_zh"],
                    "review_status": item["baseline"]["review_status"],
                    "review_status_zh": item["baseline"]["review_status_zh"],
                    "artifact_yaml": item["baseline"]["artifact_yaml"],
                    "artifact_markdown": item["baseline"]["artifact_markdown"],
                }
                for item in sections
            ],
            "excluded_material": [
                {
                    "id": "2",
                    "source_range": f"{SHEET}!B93:C93",
                    "reason": "原表明确标记为“演算式（测试しない）”，不作为测试输入基准。",
                    "disposition": "excluded",
                    "disposition_zh": "排除",
                },
                {
                    "id": "5-1",
                    "source_range": f"{SHEET}!C223:D228",
                    "reason": "asm 文不能由 WinAMS 直接执行，原表要求使用 simulator。",
                    "disposition": "excluded_from_winams_csv",
                    "disposition_zh": "不进入 WinAMS CSV",
                },
                {
                    "id": "bookkeeping",
                    "source_range": f"{SHEET}!A1:S28",
                    "reason": "目录、封面和导航信息，不是测试输入基准。",
                    "disposition": "excluded",
                    "disposition_zh": "排除",
                },
            ],
            "excluded_workbook_sheets": [
                {
                    "sheet": name,
                    "reason": "属于其他测试基准或产品；当前 Issue #6 只处理 PSD 重构。",
                    "disposition": "source_only_out_of_scope",
                    "disposition_zh": "仅保留来源范围，不参与当前映射",
                }
                for name in excluded_sheets
            ],
        },
    }


def _index_markdown(manifest: dict[str, Any]) -> str:
    source = manifest["source"]
    baseline = manifest["baseline"]
    lines = [
        "# PSD 重构 Ver.1.6 — 测试基准索引",
        "",
        "> 本索引面向人和 AI。每个测试基准使用同名 Markdown/YAML 成对保存；未列入的内容不参与当前语义规则映射。",
        "",
        f"- 来源文件：`{source['file']}`",
        f"- 来源工作表：`{source['sheet']}`",
        f"- 原文件 SHA-256：`{source['sha256']}`",
        f"- 测试基准数量：{baseline['section_count']}",
        f"- 总体复核状态：`{baseline['review_status_zh']}`",
        f"- 规则状态：{baseline['rule_status_zh']}",
        "",
        "## 已纳入的测试基准",
        "",
        "| ID | 中文基准名 | 原表名称 | 类别 | 参与生成 | 复核 | YAML | Markdown |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in baseline["sections"]:
        lines.append(
            f"| `{item['section_id']}` | {item['name']} | "
            f"{_markdown_value(item['name_original'])} | `{item['role_zh']}` | "
            f"`{item['generation_relevant_zh']}` | `{item['review_status_zh']}` | "
            f"[{item['section_id']}.yaml]({Path(item['artifact_yaml']).name}) | "
            f"[{item['section_id']}.md]({Path(item['artifact_markdown']).name}) |"
        )
    lines.extend(["", "## 已排除的内容", ""])
    for item in baseline["excluded_material"]:
        lines.append(
            f"- `{item['id']}` `{item['source_range']}`：{item['reason']} "
            f"处理：{item['disposition_zh']}（`{item['disposition']}`）。"
        )
    lines.extend(["", "## 其他工作表（当前范围外）", ""])
    for item in baseline["excluded_workbook_sheets"]:
        lines.append(f"- `{item['sheet']}`：{item['reason']}（{item['disposition_zh']}）。")
    return "\n".join(lines) + "\n"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source .xlsx file")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--out-dir", type=Path, required=True,
        help="同一目录；生成 index.md、manifest.yaml 及每个基准的 md/yaml",
    )
    return parser.parse_args()


def main() -> int:
    args = _args()
    root = args.repo_root.resolve()
    input_path = args.input.resolve()
    data = convert(input_path, root)
    source = data["source"]
    sheet = next(item for item in data["workbook"]["sheets"] if item["name"] == SHEET)
    excluded_sheets = [item["name"] for item in data["workbook"]["sheets"] if item["name"] != SHEET]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    sections: list[dict[str, Any]] = []
    for section_id, slug, rows, title_row, name_zh, category_zh, role, generation_relevant, note in SECTION_SPECS:
        yaml_path = args.out_dir / f"{slug}.yaml"
        markdown_path = args.out_dir / f"{slug}.md"
        payload = _section_payload(
            source,
            sheet,
            section_id,
            slug,
            rows,
            title_row,
            name_zh,
            category_zh,
            role,
            generation_relevant,
            note,
            _relative(yaml_path, root),
            _relative(markdown_path, root),
        )
        yaml_path.write_text("\n".join(_yaml_lines(payload)) + "\n", encoding="utf-8", newline="\n")
        markdown_path.write_text(_section_markdown(payload), encoding="utf-8", newline="\n")
        sections.append(payload)

    manifest = _manifest(source, sections, excluded_sheets)
    manifest_path = args.out_dir / "manifest.yaml"
    index_path = args.out_dir / "index.md"
    manifest_path.write_text("\n".join(_yaml_lines(manifest)) + "\n", encoding="utf-8", newline="\n")
    index_path.write_text(_index_markdown(manifest), encoding="utf-8", newline="\n")
    print(json.dumps({
        "source_sha256": source["sha256"],
        "sheet": SHEET,
        "sections": len(sections),
        "excluded_sheets": len(excluded_sheets),
        "out_dir": _relative(args.out_dir, root),
    }, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
