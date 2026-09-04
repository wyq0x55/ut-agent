"""WinAMS Golden CSV 的只读解析与语义签名。

Golden 是目标工具格式，不是 C 语义来源。这个模块只负责把已经存在的
CSV 行转换成 adapter 数据；规则层不能通过它反向解析源码。
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
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


def ordered_semantic_csv_signature(path: Path) -> dict[str, Any]:
    """Return a semantic signature that preserves CSV row order.

    ``semantic_csv_signature`` remains useful for comparing semantic coverage
    sets.  The WinAMS delivery contract also has an ordering requirement, so
    callers that validate a replayable CSV must use this stricter signature.
    Numeric formatting remains normalized, while the declared column order
    and testcase row order are retained.
    """
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
        "input_columns": tuple(parsed["input_columns"]),
        "output_columns": tuple(parsed["output_columns"]),
        "cases": tuple(cases),
    }


def normalize_label(label: str | None) -> str:
    """Normalize presentation-only label noise without changing its meaning."""
    value = "".join(str(label or "").split())
    value = re.sub(r"\((\d+)\)$", "", value)
    if value.startswith("組合せ(") and value.endswith(")"):
        value = value[len("組合せ("):-1]
        value = re.sub(r"\((\d+)\)$", "", value)
    return value


def label_kind(label: str | None, case_label: str | None = None) -> str:
    """Classify a reviewed label into a comparison viewpoint."""
    value = normalize_label(label or case_label)
    lowered = value.lower()
    if case_label or lowered.startswith("case") or lowered.startswith("default"):
        return "switch_case"
    if "=>" in value or "combination(" in lowered:
        return "condition_combination"
    if _is_true_label(value) or _is_false_label(value):
        return "branch_outcome"
    if value:
        return "branch"
    return "unlabelled"


def truth_vector(label: str | None) -> dict[str, Any] | None:
    """Decode an already-present Golden condition label.

    This is presentation normalization for the offline comparator.  It does
    not evaluate C expressions or invent a condition order; the order is the
    order explicitly written in the reviewed label.
    """
    value = normalize_label(label)
    if not value:
        return None
    if "=>" not in value:
        if value.upper() in {"TRUE", "FALSE"}:
            return {"conditions": None, "decision": value.upper() == "TRUE"}
        return None
    left, right = value.split("=>", 1)
    tokens = re.findall(r"TRUE|FALSE|T|F", left.upper())
    if not tokens:
        return None
    decision_tokens = re.findall(r"TRUE|FALSE|T|F", right.upper())
    if len(decision_tokens) != 1:
        return None
    return {
        "conditions": [token in {"TRUE", "T"} for token in tokens],
        "decision": decision_tokens[0] in {"TRUE", "T"},
    }


def _counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _column_value_classes(parsed: dict[str, Any], columns: list[str]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for column in columns:
        result[column] = _counts([
            str(item["value_classes"].get(column, "unknown"))
            for item in parsed["scenarios"]
        ])
    return result


def _stub_columns(columns: list[str]) -> list[str]:
    result = []
    for column in columns:
        compact = normalize_label(column).lower()
        if (compact.startswith("amstb_") or "callcnt" in compact
                or "ptout" in compact or "ptin" in compact
                or "arg" in compact):
            result.append(column)
    return sorted(result)


def normalize_golden_csv(path: Path) -> dict[str, Any]:
    """Return stable semantic dimensions for a reviewed WinAMS CSV.

    This is a comparison/reporting boundary only.  It never feeds a normal
    generation call and intentionally retains value classes separately from
    concrete literals so a free representative value is not confused with a
    required value.
    """
    parsed = parse_golden_csv(Path(path))
    all_columns = parsed["input_columns"] + parsed["output_columns"]
    stub_columns = _stub_columns(all_columns)
    cases = []
    viewpoint_labels: list[str] = []
    for item in parsed["scenarios"]:
        kind = label_kind(item["label"], item["case_label"])
        label = normalize_label(item["label"] or item["case_label"])
        if label:
            viewpoint_labels.append(label)
        strict_inputs = {
            key: item["inputs"].get(key)
            for key in parsed["input_columns"]
            if kind == "switch_case"
            or item["value_classes"].get(key) not in {"literal", "pointer-address"}
        }
        vector = truth_vector(label)
        stub_values = {
            key: item["inputs"].get(key) for key in stub_columns
        }
        pre_state = {
            key: value for key, value in item["inputs"].items()
            if key not in stub_columns
        }
        cases.append({
            "case_id": item["case_id"],
            "branch_index": item["branch_index"],
            "kind": kind,
            "label": label,
            "outcome": item["outcome"],
            "truth_vector": vector,
            "inputs": dict(item["inputs"]),
            "expected": dict(item["expected"]),
            "raw_inputs": dict(item["raw_inputs"]),
            "raw_expected": dict(item["raw_expected"]),
            "input_value_classes": {
                key: item["value_classes"].get(key, "unknown")
                for key in parsed["input_columns"]
            },
            "expected_value_classes": {
                key: item["value_classes"].get(key, "unknown")
                for key in parsed["output_columns"]
            },
            "required_input_values": strict_inputs,
            "required_expected_values": {
                key: item["expected"].get(key)
                for key in parsed["output_columns"]
            },
            "stub": {
                "columns": list(stub_columns),
                "values": stub_values,
            },
            "pre_state": pre_state,
            "oracle": {
                "columns": list(parsed["output_columns"]),
                "values": dict(item["expected"]),
            },
            "provenance": {
                "source": "reviewed Golden",
                "path": str(Path(path).resolve()),
            },
        })
    return {
        "input_columns": list(parsed["input_columns"]),
        "output_columns": list(parsed["output_columns"]),
        "testcase_count": len(parsed["scenarios"]),
        "viewpoints": {
            "counts": _counts([item["kind"] for item in cases]),
            "labels": sorted(set(viewpoint_labels)),
        },
        "condition_combinations": sorted({
            item["label"] for item in cases
            if item["kind"] == "condition_combination" and item["label"]
        }),
        "boundary_domain": {
            "input_value_classes": _column_value_classes(
                parsed, parsed["input_columns"]
            ),
            "expected_value_classes": _column_value_classes(
                parsed, parsed["output_columns"]
            ),
            "all_value_classes": _counts([
                str(value_class)
                for item in parsed["scenarios"]
                for value_class in item["value_classes"].values()
            ]),
        },
        "stub": {
            "declaration_count": len(parsed["stub_declarations"]),
            "declarations": sorted({
                str(row[1]) for row in parsed["stub_declarations"]
                if len(row) > 1
            }),
            "columns": _stub_columns(all_columns),
        },
        "oracle": {
            "output_count": parsed["output_count"],
            "columns": list(parsed["output_columns"]),
        },
        "required_values": {
            "inputs": [{
                "kind": item["kind"], "label": item["label"],
                "outcome": item["outcome"],
                "values": item["required_input_values"],
            } for item in cases if item["required_input_values"]],
            "expected": [{
                "kind": item["kind"], "label": item["label"],
                "outcome": item["outcome"],
                "values": item["required_expected_values"],
            } for item in cases],
        },
        "projection": {
            "input_count": parsed["input_count"],
            "output_count": parsed["output_count"],
            "comment_columns": list(all_columns),
            "observed_label_count": len(parsed["observed_labels"]),
            "scenario_count": len(parsed["scenarios"]),
        },
        "cases": cases,
    }


__all__ = [
    "label_kind", "normalize_golden_csv", "normalize_label",
    "ordered_semantic_csv_signature", "parse_golden_csv",
    "semantic_csv_signature", "truth_vector",
]
