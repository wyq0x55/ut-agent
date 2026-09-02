"""把语料中的函数级模式压缩为可审查的语义族。

该模块只读取 ``rules collect`` 产生的 JSON，不读取或生成 TestCsv。
它不会把单函数模板自动晋升为通用规则；晋升级别由出现的函数数和项目数
决定，方便执行 leave-one-project-out 验证。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _branch_family(branch: dict[str, Any]) -> dict[str, Any]:
    """丢弃变量名/字面量，只保留可跨函数比较的分支语义。"""
    atoms = [
        {
            "op": str(atom.get("op", "")),
            "boundary_class": str(atom.get("boundary_class", "unknown")),
            "masked": bool(atom.get("masked", False)),
            "mask_width": atom.get("mask_width"),
        }
        for atom in branch.get("atoms", [])
    ]
    atoms.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return {
        "kind": str(branch.get("kind", "")),
        "connective": str(branch.get("connective", "single")),
        "atom_count": len(atoms),
        "atoms": atoms,
    }


def _project_id(corpus: dict[str, Any], fallback: str | None) -> str:
    if fallback:
        return fallback
    roots = corpus.get("roots", {})
    for value in roots.values():
        parts = Path(str(value)).parts
        for part in reversed(parts):
            if part.startswith("N-O"):
                return part
    return "unknown-project"


def compress_corpus(
    corpus: dict[str, Any],
    *,
    project_id: str | None = None,
    min_functions: int = 2,
    min_projects: int = 2,
) -> dict[str, Any]:
    """输出跨函数/跨项目语义族及候选规则。

    ``samples`` 中的完整 scenario_matrix 永远只作为 evidence 保留；本函数
    生成的 ``semantic_family`` 规则不包含历史输入行，因此不能回放项目 CSV。
    """
    project = _project_id(corpus, project_id)
    groups: dict[str, dict[str, Any]] = {}
    for sample in corpus.get("samples", []):
        facts = sample.get("source_facts", {})
        pattern = facts.get("pattern", {})
        shape = pattern.get("shape", {})
        function = str(sample.get("function", ""))
        if not function or not shape:
            continue
        for branch in shape.get("branches", []):
            family = _branch_family(branch)
            family_id = f"family.{_digest(family)}"
            item = groups.setdefault(
                family_id,
                {"family_id": family_id, "signature": family,
                 "functions": set(), "projects": set(), "examples": []},
            )
            item["functions"].add(function)
            item["projects"].add(str(sample.get("project_id", project)))
            example = f"{sample.get('source_rel', '')}::{function}"
            if example not in item["examples"] and len(item["examples"]) < 20:
                item["examples"].append(example)

    families: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    for family_id in sorted(groups):
        item = groups[family_id]
        functions = sorted(item["functions"])
        projects = sorted(item["projects"])
        if len(projects) >= min_projects and len(functions) >= min_functions:
            classification = "CROSS_PROJECT"
        elif len(functions) >= min_functions:
            classification = "CROSS_FUNCTION"
        else:
            classification = "PROJECT_SPECIFIC"
        family = {
            "family_id": family_id,
            "signature": item["signature"],
            "function_count": len(functions),
            "project_count": len(projects),
            "functions": functions,
            "projects": projects,
            "classification": classification,
            "examples": sorted(item["examples"]),
        }
        families.append(family)
        rules.append({
            "id": f"semantic.{family_id.rsplit('.', 1)[-1]}",
            "status": "candidate",
            "scope": {"function": "*"},
            "match": {"kind": "semantic_family", "family_id": family_id},
            "action": {
                "strategy": "instantiate-from-ast",
                "signature": item["signature"],
                "classification": classification,
            },
            "priority": 40,
            "evidence": [
                f"functions:{len(functions)}",
                f"projects:{len(projects)}",
                *[f"example:{value}" for value in sorted(item["examples"])[:5]],
            ],
            "approval": {},
        })

    profile_rules = [
        rule for rule in corpus.get("candidate_pack", {}).get("rules", [])
        if rule.get("match", {}).get("kind") == "profile_strategy"
    ]
    return {
        "schema_version": 1,
        "kind": "ut-agent-compressed-rule-corpus",
        "project": project,
        "source_corpus": corpus.get("kind", ""),
        "thresholds": {
            "min_functions": min_functions,
            "min_projects": min_projects,
        },
        "families": families,
        "candidate_pack": {
            "name": f"{corpus.get('candidate_pack', {}).get('name', 'corpus')}-compressed",
            "version": 1,
            "profile": corpus.get("candidate_pack", {}).get("profile", {}),
            "samples_are_evidence_only": True,
            "rules": rules + profile_rules,
        },
        "counts": {
            "families": len(families),
            "project_specific": sum(item["classification"] == "PROJECT_SPECIFIC" for item in families),
            "cross_function": sum(item["classification"] == "CROSS_FUNCTION" for item in families),
            "cross_project": sum(item["classification"] == "CROSS_PROJECT" for item in families),
            "profile_rules": len(profile_rules),
        },
    }


def compress_corpus_file(
    corpus_path: Path,
    output: Path,
    *,
    project_id: str | None = None,
    min_functions: int = 2,
    min_projects: int = 2,
) -> dict[str, Any]:
    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    result = compress_corpus(
        corpus,
        project_id=project_id,
        min_functions=min_functions,
        min_projects=min_projects,
    )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return result


def compress_corpora(
    corpora: list[dict[str, Any]],
    *,
    project_ids: list[str] | None = None,
    min_functions: int = 2,
    min_projects: int = 2,
) -> dict[str, Any]:
    """合并多个项目语料后压缩，形成跨项目晋升依据。"""
    if not corpora:
        raise ValueError("至少需要一个语料报告")
    ids = project_ids or []
    if ids and len(ids) != len(corpora):
        raise ValueError("--project-id 数量必须与 corpus 数量一致")
    samples: list[dict[str, Any]] = []
    for index, corpus in enumerate(corpora):
        project = ids[index] if ids else _project_id(corpus, None)
        for sample in corpus.get("samples", []):
            copy = dict(sample)
            copy["project_id"] = project
            samples.append(copy)
    merged = dict(corpora[0])
    merged["samples"] = samples
    merged["candidate_pack"] = dict(corpora[0].get("candidate_pack", {}))
    profile_rules: dict[str, dict[str, Any]] = {}
    for corpus in corpora:
        for rule in corpus.get("candidate_pack", {}).get("rules", []):
            if rule.get("match", {}).get("kind") == "profile_strategy":
                profile_rules[str(rule.get("id"))] = rule
    merged["candidate_pack"]["rules"] = list(profile_rules.values())
    result = compress_corpus(
        merged,
        project_id=ids[0] if len(corpora) == 1 and ids else None,
        min_functions=min_functions,
        min_projects=min_projects,
    )
    result["source_corpora"] = [
        str(corpus.get("roots", {}).get("winams", "")) for corpus in corpora
    ]
    result["candidate_pack"]["profile"] = merged["candidate_pack"].get("profile", {})
    return result
