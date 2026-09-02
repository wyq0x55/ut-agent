"""从 Golden 数据生成待审批的确定性候选规则。

规则学习只消费两类输入：严格解析的 FunctionIR 事实和目标适配器提供的
Golden 行。它不读取 C 文件，不解析条件文本，也不按类型名称猜测语义。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ut_agent.ir import FunctionIR
from ut_agent.winams.golden import parse_golden_csv, semantic_csv_signature


def _exact_bindings(ir: FunctionIR, columns: list[str]) -> dict[str, str]:
    """Bind only an IR spelling that exactly exists in the adapter columns."""
    facts: set[str] = set()
    facts.update(str(param.name) for param in ir.params if param.name)
    facts.update(str(control.name) for control in ir.control_vars if control.name)
    facts.update(str(control.var) for control in ir.control_vars if control.var)
    for branch in ir.branches:
        facts.update(str(atom.var) for atom in branch.atoms if atom.var)
        if branch.selector:
            facts.add(str(branch.selector.expression))
    return {
        fact: fact for fact in sorted(facts)
        if fact in columns
    }


def _exact_output_bindings(ir: FunctionIR, columns: list[str]) -> dict[str, str]:
    facts: set[str] = set(str(item) for item in ir.global_writes if item)
    for effect in ir.global_write_effects:
        path = getattr(effect, "path", None)
        if path:
            facts.add(str(path))
    for param in ir.params:
        for path in param.access_paths:
            if getattr(path, "write", False) and getattr(path, "path", None):
                facts.add(str(path.path))
    return {
        fact: fact for fact in sorted(facts)
        if fact in columns
    }


def _rule_evidence(parsed: dict[str, Any]) -> dict[str, Any]:
    labels = parsed["observed_labels"]
    combinations = sorted({
        label for label in labels
        if "=>" in "".join(label.split())
        or label.lstrip().startswith("組合せ(TRUE")
        or label.lstrip().startswith("組合せ(FALSE")
    })
    cases = sorted({
        label.strip() for label in labels
        if label.strip().lower().startswith("case ")
        or label.strip().lower().startswith("default")
    })
    value_classes: dict[str, set[str]] = {
        name: set() for name in (
            parsed["input_columns"] + parsed["output_columns"]
        )
    }
    for scenario in parsed["scenarios"]:
        for name in {**scenario["inputs"], **scenario["expected"]}:
            if name in value_classes:
                raw_class = scenario["value_classes"].get(name)
                if raw_class:
                    value_classes[name].add(raw_class)
    return {
        "observed_label_count": len(labels),
        "mcdc_combinations": combinations,
        "case_labels": cases,
        "input_value_classes": {
            name: sorted(values) for name, values in value_classes.items()
        },
    }


def infer_rule_pack(ir: FunctionIR, golden: Path) -> dict[str, Any]:
    """Create a candidate pack; no candidate is implicitly approved."""
    target = Path(golden)
    parsed = parse_golden_csv(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    action = {
        "input_columns": list(parsed["input_columns"]),
        "output_columns": list(parsed["output_columns"]),
        "bindings": _exact_bindings(ir, list(parsed["input_columns"])),
        "derived_bindings": {},
        "output_bindings": _exact_output_bindings(
            ir, list(parsed["output_columns"])
        ),
        "golden_branch_labels": list(parsed["branch_labels"]),
        "golden_outcome_labels": [
            parsed["golden_outcome_labels"].get(index, [])
            for index in range(len(parsed["branch_labels"]))
        ],
        "stub_declarations": list(parsed["stub_declarations"]),
        "rule_evidence": _rule_evidence(parsed),
        "scenarios": list(parsed["scenarios"]),
    }
    return {
        "name": f"{ir.name}-inferred",
        "version": 1,
        "rules": [{
            "id": f"project.{ir.name}.scenarios",
            "status": "candidate",
            "scope": {"function": ir.name},
            "match": {"kind": "scenario_matrix"},
            "action": action,
            "priority": 10,
            "evidence": [f"sha256:{digest}", str(target)],
            "approval": {},
        }],
    }


__all__ = ["infer_rule_pack", "semantic_csv_signature"]
