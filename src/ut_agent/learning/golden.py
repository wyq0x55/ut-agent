"""WinAMS Golden CSV 的只读解析与语义签名。

Golden 是目标工具格式，不是 C 语义来源。这个模块只负责把已经存在的
CSV 行转换成 adapter 数据；规则层不能通过它反向解析源码。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any


def _value(text: str) -> Any:
    stripped = text.strip()
    try:
        return int(stripped, 0)
    except ValueError:
        try:
            return int(stripped)
        except ValueError:
            return stripped


def _value_class(value: Any) -> str:
    if not isinstance(value, int):
        return "symbol"
    if value == 0:
        return "zero"
    if value == 1:
        return "one"
    if value in (0xFF, 0xFFFF, 0xFFFFFFFF):
        return "type-max"
    if 0x1000 <= value <= 0xFFFF and value % 0x100 == 0:
        return "pointer-address"
    return "literal"


def _compact(value: str) -> str:
    return "".join(str(value or "").split())


def _is_true_label(label: str) -> bool:
    compact = _compact(label).upper()
    return label.startswith("TRUE") or "=>T" in compact \
        or "組合せ(TRUE" in label.upper()


def _is_false_label(label: str) -> bool:
    compact = _compact(label).upper()
    return label.startswith("FALSE") or "=>F" in compact \
        or "組合せ(FALSE" in label.upper()


def _is_case_label(label: str) -> bool:
    stripped = label.strip().lower()
    return stripped.startswith("case ") or stripped.startswith("default")


def parse_golden_csv(path: Path) -> dict[str, Any]:
    """Read a Golden CSV without consulting the source tree."""
    target = Path(path)
    rows = list(csv.reader(io.StringIO(target.read_bytes().decode("cp932"))))
    if not rows or not rows[0] or rows[0][0] != "mod":
        raise ValueError(f"不是 WinAMS TestCsv: {target}")
    try:
        input_count = int(rows[0][3])
        output_count = int(rows[0][4])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"WinAMS mod 行缺少输入/输出列数：{target}") from exc
    comments = next(
        (row[1:] for row in rows if row and row[0] == "#COMMENT"), []
    )
    if len(comments) < input_count + output_count:
        raise ValueError("#COMMENT 列数少于 mod 声明")

    current_branch: int | None = None
    current_outcome: bool | None = None
    current_case: str | None = None
    current_vector_label: str | None = None
    observed_labels: list[str] = []
    branch_labels: list[str] = []
    golden_outcome_labels: dict[int, list[str]] = {}
    stub_declarations: list[list[str]] = []
    scenarios: list[dict[str, Any]] = []

    for row in rows:
        if row and row[0] == "%":
            stub_declarations.append(list(row))
            continue
        if row and row[0] == ";$L$" and len(row) > 1:
            label = row[1]
            observed_labels.append(label)
            if _is_true_label(label):
                current_outcome = True
                current_vector_label = label
                if current_branch is not None:
                    golden_outcome_labels.setdefault(current_branch, []).append(label)
            elif _is_false_label(label):
                current_outcome = False
                current_vector_label = label
                if current_branch is not None:
                    golden_outcome_labels.setdefault(current_branch, []).append(label)
            elif label.lstrip().startswith("組合せ("):
                current_vector_label = label
                if current_branch is not None:
                    golden_outcome_labels.setdefault(current_branch, []).append(label)
            elif _is_case_label(label):
                current_case = label.strip()
                current_outcome = None
                current_vector_label = label
            else:
                current_branch = len(branch_labels)
                current_outcome = None
                current_case = None
                current_vector_label = None
                branch_labels.append(label)
            continue
        if not row or row[0] != "" or len(row) <= 1:
            continue
        cells = [_value(item) for item in row[1:1 + input_count + output_count]]
        input_columns = comments[:input_count]
        output_columns = comments[input_count:input_count + output_count]
        inputs = dict(zip(input_columns, cells[:input_count]))
        expected = dict(zip(output_columns, cells[input_count:]))
        scenarios.append({
            "case_id": f"U{len(scenarios) + 1:03d}",
            "branch_index": current_branch,
            "outcome": current_outcome,
            "kind": "case" if current_case else "scenario",
            "case_label": current_case,
            "label": current_vector_label,
            "inputs": inputs,
            "expected": expected,
            "raw_inputs": dict(zip(
                input_columns, row[1:1 + input_count],
            )),
            "raw_expected": dict(zip(
                output_columns, row[1 + input_count:1 + input_count + output_count],
            )),
            "value_classes": {
                key: _value_class(value) for key, value in {**inputs, **expected}.items()
            },
        })
    return {
        "input_count": input_count,
        "output_count": output_count,
        "input_columns": comments[:input_count],
        "output_columns": comments[input_count:input_count + output_count],
        "observed_labels": observed_labels,
        "branch_labels": branch_labels,
        "golden_outcome_labels": golden_outcome_labels,
        "stub_declarations": stub_declarations,
        "scenarios": scenarios,
    }


def semantic_csv_signature(path: Path) -> dict[str, Any]:
    """Return a row-order and numeric-format independent Golden signature."""
    parsed = parse_golden_csv(path)
    cases = []
    for item in parsed["scenarios"]:
        values = {**item["inputs"], **item["expected"]}
        typed = tuple(sorted(
            (comment, _value_class(value), value)
            for comment, value in values.items()
        ))
        cases.append((
            item["label"] or item["case_label"] or "ENTRY",
            "TRUE" if item["outcome"] is not False else "FALSE",
            typed,
        ))
    return {
        "input_columns": tuple(sorted(parsed["input_columns"])),
        "output_columns": tuple(sorted(parsed["output_columns"])),
        "cases": tuple(sorted(cases, key=repr)),
    }
