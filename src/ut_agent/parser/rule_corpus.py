"""离线收集外部测试记录并归纳候选规则。

这是 parser 侧的批处理编排入口，不参与正常 ``gen``/``project`` 生成。
它负责发现源码、建立 CompileContext、调用 C++ extractor，并把得到的
FunctionIR 与外部测试记录交给规则层；规则层本身不拥有这些文件操作。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from ut_agent.parser import (
    ClangExtractor,
    default_clang_extractor,
    discover_compile_sources,
    make_compile_context,
)
from ut_agent.winams.rule_infer import infer_rule_pack


def _posix(path: Path) -> str:
    return path.as_posix()


def _short_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def infer_source_root(winams_root: Path) -> Path:
    """按 ``work/winAMS/src/bsw`` 的约定推导同级 ``Soft/src/bsw``。"""
    root = Path(winams_root).resolve()
    if root.name.lower() != "bsw" or root.parent.name.lower() != "src":
        raise ValueError(
            "无法自动推导 Soft 根目录；请显式指定 --source-root（期望 winAMS/src/bsw）"
        )
    work = root.parent.parent.parent
    return (work / "Soft" / "src" / "bsw").resolve()


def discover_include_dirs(root: Path) -> tuple[Path, ...]:
    """返回稳定排序的源码/include 目录。"""
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"include 根目录不存在：{root}")
    dirs = {root}
    dirs.update(item for item in root.rglob("*") if item.is_dir())
    return tuple(sorted(dirs, key=lambda item: item.as_posix().lower()))


def _csv_samples(winams_root: Path) -> list[dict[str, Any]]:
    root = Path(winams_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"winAMS 样本根目录不存在：{root}")
    out: list[dict[str, Any]] = []
    for csv_path in sorted(
        (item for item in root.rglob("*.csv")
         if item.parent.name.lower() == "testcsv"),
        key=lambda item: item.as_posix().lower(),
    ):
        function_dir = csv_path.parent.parent
        source_container = function_dir.parent
        try:
            source_rel = source_container.relative_to(root)
        except ValueError:
            continue
        out.append({
            "golden": csv_path,
            "function": function_dir.name,
            "source_rel": _posix(source_rel),
            "golden_rel": _posix(csv_path.relative_to(root)),
        })
    return out


def discover_samples(winams_root: Path, source_root: Path | None = None) -> list[dict[str, Any]]:
    """发现 ``<source>.c/<function>/TestCsv/*.csv`` 样本并配对源码。"""
    winams_root = Path(winams_root).resolve()
    source_root = Path(source_root).resolve() if source_root else infer_source_root(winams_root)
    samples = _csv_samples(winams_root)
    for item in samples:
        source = source_root / Path(item["source_rel"])
        item["source"] = source
        item["source_exists"] = source.is_file()
    return samples


def _boundary_class(atom: Any) -> str:
    if getattr(atom, "boundary_name", None):
        return "enum-or-macro"
    value = getattr(atom, "boundary", None)
    if value == 0:
        return "zero"
    if value == 1:
        return "one"
    if value is None:
        return "unknown"
    return "literal"


def semantic_pattern(ir: Any) -> dict[str, Any]:
    """生成不含函数名/变量名的结构签名，用于跨函数聚类。"""
    branches = []
    for branch in ir.branches:
        atoms = []
        for atom in branch.atoms:
            atoms.append({
                "op": atom.op,
                "type": atom.var_type or "unknown",
                "boundary_class": _boundary_class(atom),
                "masked": atom.mask is not None,
                "mask_width": (
                    int(atom.mask).bit_length() if atom.mask is not None else None
                ),
            })
        branches.append({
            "kind": branch.kind,
            "connective": branch.connective or "single",
            "constant": branch.constant_value,
            "atoms": sorted(atoms, key=lambda item: json.dumps(item, sort_keys=True)),
        })
    calls = sorted(
        ("pointer" if call.ptr_call else "direct", bool(call.ret_type and call.ret_type != "void"))
        for call in ir.calls
    )
    shape = {
        "return": "void" if ir.ret_type == "void" else "scalar",
        "pointer_params": sum(1 for param in ir.params if param.is_ptr),
        "written_pointer_params": sum(
            1 for param in ir.params if param.is_ptr and param.is_written
        ),
        "branches": branches,
        "calls": calls,
        "memory_reads": sum(1 for item in ir.memory_vars if item.read),
        "memory_writes": sum(1 for item in ir.memory_vars if item.write),
        "global_writes": len(ir.global_writes),
    }
    canonical = json.dumps(shape, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"pattern_id": f"pattern.{_short_digest(canonical)}", "shape": shape}


def _source_summary(ir: Any) -> dict[str, Any]:
    return {
        "function": ir.name,
        "file": str(ir.file),
        "line": ir.line,
        "ret_type": ir.ret_type,
        "params": len(ir.params),
        "branches": len(ir.branches),
        "atoms": sum(len(branch.atoms) for branch in ir.branches),
        "calls": len(ir.calls),
        "notes": list(ir.notes),
        "pattern": semantic_pattern(ir),
    }


def _unique_rule_id(rule: dict[str, Any], sample: dict[str, Any]) -> None:
    old = str(rule.get("id", "candidate.scenarios"))
    suffix = _short_digest(f"{sample['source_rel']}::{sample['function']}")
    rule["id"] = f"corpus.{suffix}.{old.rsplit('.', 1)[-1]}"
    rule.setdefault("scope", {})["function"] = sample["function"]
    evidence = list(rule.get("evidence", []))
    evidence.append(f"source:{sample['source_rel']}")
    rule["evidence"] = sorted(set(evidence))


def _pattern_rule(pattern: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    pattern_id = pattern["pattern_id"]
    examples = sorted({f"{item['source_rel']}::{item['function']}" for item in samples})
    return {
        "id": f"generic.{pattern_id.rsplit('.', 1)[-1]}",
        "status": "candidate",
        "scope": {"function": "*"},
        "match": {"kind": "semantic_pattern", "pattern_id": pattern_id},
        "action": {
            "strategy": "review-before-instantiation",
            "template": pattern["shape"],
            "occurrences": len(examples),
            "examples": examples[:20],
        },
        "priority": 50,
        "evidence": [f"corpus-occurrences:{len(examples)}"],
        "approval": {},
    }


def _derived_profile_rules(records: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """把函数级 CSV 观察归纳为 Profile 级候选规则。

    这些规则描述测试观点的选择策略，而不是某个函数的具体输入行；保持
    ``candidate`` 是为了让审批人能逐条确认是否可跨函数复用。
    """
    evidence = [item.get("rule_evidence", {}) for item in records
                if item.get("status") == "INFERRED"]
    if not evidence:
        return []
    rules: list[dict[str, Any]] = []
    if any(item.get("mcdc_combinations") for item in evidence):
        connectors = sorted({
            connector
            for item in evidence
            for label in item.get("mcdc_combinations", [])
            for connector in ("&&", "||")
            if connector in label
        })
        rules.append({
            "id": f"profile.{profile['profile_version']}.mcdc",
            "status": "candidate",
            "scope": {"function": "*"},
            "match": {"kind": "profile_strategy", "strategy": "mcdc"},
            "action": {
                "strategy": "independent-atomic-combinations",
                "enabled": bool(profile["mcdc_enabled"]),
                "connectives_observed": connectors,
                "evidence_samples": len(evidence),
            },
            "priority": 30,
            "evidence": ["history:TestCsv labels", "基准书:PSD再構築/4-4"],
            "approval": {},
        })
    case_labels = sorted({
        label for item in evidence for label in item.get("case_labels", [])
    })
    if case_labels:
        rules.append({
            "id": f"profile.{profile['profile_version']}.switch-case",
            "status": "candidate",
            "scope": {"function": "*"},
            "match": {"kind": "profile_strategy", "strategy": "switch-case"},
            "action": {
                "strategy": "preserve-case-semantics",
                "default_supported": any(label.lower().startswith("default")
                                          for label in case_labels),
                "observed_case_labels": case_labels,
            },
            "priority": 30,
            "evidence": ["history:TestCsv case labels", "基准书:PSD再構築/6-1"],
            "approval": {},
        })
    value_classes = sorted({
        value_class
        for item in evidence
        for classes in item.get("input_value_classes", {}).values()
        for value_class in classes
    })
    if value_classes:
        rules.append({
            "id": f"profile.{profile['profile_version']}.boundary-values",
            "status": "candidate",
            "scope": {"function": "*"},
            "match": {"kind": "profile_strategy", "strategy": "boundary-values"},
            "action": {
                "strategy": "typed-boundary-and-domain-extremes",
                "observed_value_classes": value_classes,
            },
            "priority": 30,
            "evidence": ["history:TestCsv value classes", "基准书:PSD再構築/0-2,4-2~4-3"],
            "approval": {},
        })
    return rules


def collect_rule_corpus(
    winams_root: Path,
    *,
    base_profile: str = "PSD再構築",
    profile_version: str = "PSD再構築-v1",
    mcdc_enabled: bool = True,
    approved_exceptions: Sequence[str] | None = None,
    source_root: Path | None = None,
    include_root: Path | None = None,
    include_dirs: Sequence[Path] | None = None,
    defines: dict[str, str] | None = None,
    force_include: Path | None = None,
    output: Path | None = None,
    candidate_pack: Path | None = None,
    max_samples: int | None = None,
) -> dict[str, Any]:
    """执行一次完整离线采集，返回并可写出 corpus/候选规则报告。"""
    winams_root = Path(winams_root).resolve()
    source_root = Path(source_root).resolve() if source_root else infer_source_root(winams_root)
    include_root = Path(include_root).resolve() if include_root else source_root.parent
    samples = discover_samples(winams_root, source_root)
    if max_samples is not None:
        samples = samples[:max_samples]
    # Full-project recursive discovery is useful for a one-off corpus scan,
    # but large embedded projects can contain thousands of directories and
    # make every clang invocation unnecessarily expensive.  Callers that have
    # the project's compile/include contract should pass the stable, concise
    # list explicitly; the legacy include_root behavior remains the default.
    resolved_include_dirs = tuple(
        sorted(
            {Path(item).resolve() for item in include_dirs},
            key=lambda item: item.as_posix().lower(),
        )
        if include_dirs is not None
        else discover_include_dirs(include_root)
    )
    defines = dict(defines or {})

    extractor = ClangExtractor(default_clang_extractor())
    source_cache: dict[Path, tuple[Any | None, str | None]] = {}
    records: list[dict[str, Any]] = []
    inferred_rules: list[dict[str, Any]] = []
    pattern_groups: dict[str, list[dict[str, Any]]] = {}

    for sample in samples:
        source = Path(sample["source"])
        record: dict[str, Any] = {
            "sample_id": f"{sample['source_rel']}::{sample['function']}",
            "function": sample["function"],
            "source_rel": sample["source_rel"],
            "golden_rel": sample["golden_rel"],
            "source": str(source),
            "golden": str(sample["golden"]),
            "status": "UNSUPPORTED",
            "errors": [],
        }
        if not source.is_file():
            record["errors"].append(f"源码不存在: {source}")
            records.append(record)
            continue
        try:
            if source not in source_cache:
                try:
                    source_cache[source] = (
                        make_compile_context(
                            discover_compile_sources(source_root, source),
                            resolved_include_dirs,
                            defines,
                            [force_include] if force_include else (),
                        ),
                        None,
                    )
                except Exception as exc:  # 单个源文件失败不阻塞整批采集
                    source_cache[source] = (None, f"{type(exc).__name__}: {exc}")
            context, parse_error = source_cache[source]
            if context is None:
                raise RuntimeError(parse_error or "C++ Clang 源码解析失败")
            ir = extractor.extract_from_source(
                context, sample["function"], source, cwd=source.parent
            )
            record["source_facts"] = _source_summary(ir)
            pattern = record["source_facts"]["pattern"]
            pattern_groups.setdefault(pattern["pattern_id"], []).append(record)
            inferred = infer_rule_pack(ir, Path(sample["golden"]))
            rule = dict(inferred["rules"][0])
            _unique_rule_id(rule, sample)
            inferred_rules.append(rule)
            action = rule.get("action", {})
            record.update({
                "status": "INFERRED",
                "scenario_count": len(action.get("scenarios", [])),
                "input_columns": list(action.get("input_columns", [])),
                "output_columns": list(action.get("output_columns", [])),
                "rule_evidence": dict(action.get("rule_evidence", {})),
                "candidate_rule_id": rule["id"],
            })
        except Exception as exc:  # noqa: BLE001 —— 批量采集必须继续并留下证据
            record["errors"].append(f"{type(exc).__name__}: {exc}")
        records.append(record)

    pattern_rules = []
    for pattern_id in sorted(pattern_groups):
        items = pattern_groups[pattern_id]
        pattern = next(item["source_facts"]["pattern"] for item in items)
        pattern_rules.append(_pattern_rule(pattern, items))

    project_rules = sorted(inferred_rules, key=lambda item: str(item["id"]))
    all_rules = project_rules + pattern_rules
    profile = {
        "base_profile": base_profile,
        "profile_version": profile_version,
        "mcdc_enabled": bool(mcdc_enabled),
        "approved_exceptions": sorted(set(approved_exceptions or ())),
    }
    candidate = {
        "name": f"{winams_root.name}-corpus-candidates",
        "version": 1,
        "profile": profile,
        "samples_are_evidence_only": True,
        "rules": all_rules + _derived_profile_rules(records, profile),
    }
    derived_rules = [
        item for item in candidate["rules"]
        if str(item.get("id", "")).startswith("profile.")
    ]
    counts = {
        "samples": len(records),
        "inferred": sum(item["status"] == "INFERRED" for item in records),
        "unsupported": sum(item["status"] == "UNSUPPORTED" for item in records),
        "patterns": len(pattern_rules),
        "derived_rules": len(derived_rules),
        "candidate_rules": len(candidate["rules"]),
    }
    result = {
        "schema_version": 1,
        "kind": "ut-agent-rule-corpus",
        "roots": {
            "evidence": str(winams_root),
            "source": str(source_root),
            "include": str(include_root),
        },
        "config": {"defines": defines, "force_include": str(force_include) if force_include else None},
        "counts": counts,
        "samples": sorted(records, key=lambda item: item["sample_id"]),
        "patterns": [
            {
                "pattern_id": rule["match"]["pattern_id"],
                "occurrences": rule["action"]["occurrences"],
                "examples": rule["action"]["examples"],
                "template": rule["action"]["template"],
            }
            for rule in pattern_rules
        ],
        "candidate_pack": candidate,
    }
    if output is not None:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if candidate_pack is not None:
        candidate_pack = Path(candidate_pack)
        candidate_pack.parent.mkdir(parents=True, exist_ok=True)
        candidate_pack.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
