"""版本化规则包的读取、校验与匹配。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Rule:
    rule_id: str
    status: str
    scope: dict[str, str]
    match: dict[str, Any]
    action: dict[str, Any]
    priority: int = 100
    evidence: tuple[str, ...] = ()
    approval: dict[str, str] = field(default_factory=dict)

    def matches(self, function: str, kind: str | None = None) -> bool:
        scoped = self.scope.get("function", "*")
        if scoped not in ("*", function):
            return False
        expected_kind = self.match.get("kind")
        return kind is None or expected_kind in (None, kind)


@dataclass(frozen=True)
class RulePack:
    name: str
    version: int
    rules: tuple[Rule, ...]
    source: str = "builtin"

    def approved(self, function: str, kind: str | None = None) -> tuple[Rule, ...]:
        matched = [
            rule for rule in self.rules
            if rule.status == "approved" and rule.matches(function, kind)
        ]
        return tuple(sorted(matched, key=lambda item: (item.priority, item.rule_id)))


BUILTIN_PACK = RulePack(
    name="builtin",
    version=1,
    rules=(
        Rule("builtin.compare", "approved", {"function": "*"},
             {"kind": "comparison"}, {"strategy": "typed-boundary"}, 100,
             ("FunctionIR.Atom",), {"authority": "project-spec"}),
        Rule("builtin.pointer", "approved", {"function": "*"},
             {"kind": "pointer"}, {"strategy": "valid-address"}, 100,
             ("target address contract",), {"authority": "project-spec"}),
        Rule("builtin.memory", "approved", {"function": "*"},
             {"kind": "oracle"}, {"strategy": "source-memory-ops"}, 100,
             ("FunctionIR.MemoryVar",), {"authority": "project-spec"}),
        Rule("builtin.bitmask", "approved", {"function": "*"},
             {"kind": "bitmask"},
             {"strategy": "mask-hit-all-hit-no-hit"}, 90,
             ("FunctionIR.Atom.text",), {"authority": "project-spec"}),
        Rule("builtin.state", "approved", {"function": "*"},
             {"kind": "state"},
             {"strategy": "valid-invalid-low-invalid-high"}, 100,
             ("typed control variable",), {"authority": "project-spec"}),
    ),
)


def _pack_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """返回规则包字段所在层，兼容压缩报告的 ``candidate_pack``。"""
    nested = raw.get("candidate_pack")
    if "rules" not in raw and isinstance(nested, dict):
        return nested
    return raw


def _parse_rule(raw: dict[str, Any]) -> Rule:
    missing = {"id", "status", "scope", "match", "action"} - raw.keys()
    if missing:
        raise ValueError(f"规则缺少字段: {sorted(missing)}")
    status = str(raw["status"])
    if status not in {"candidate", "approved", "rejected"}:
        raise ValueError(f"规则 {raw['id']} 状态无效: {status}")
    if status == "approved" and not raw.get("approval"):
        raise ValueError(f"已批准规则 {raw['id']} 缺少 approval 记录")
    return Rule(
        rule_id=str(raw["id"]), status=status,
        scope={str(k): str(v) for k, v in dict(raw["scope"]).items()},
        match=dict(raw["match"]), action=dict(raw["action"]),
        priority=int(raw.get("priority", 100)),
        evidence=tuple(str(item) for item in raw.get("evidence", [])),
        approval={str(k): str(v) for k, v in dict(raw.get("approval", {})).items()},
    )


def validate_pack(pack: RulePack) -> RulePack:
    seen: set[str] = set()
    signatures: set[tuple[str, str, str, int]] = set()
    for rule in pack.rules:
        if rule.rule_id in seen:
            raise ValueError(f"重复规则 ID: {rule.rule_id}")
        seen.add(rule.rule_id)
        if rule.status != "approved":
            continue
        # Different semantic families/profile strategies may share a kind and
        # priority; only the same selector is ambiguous.
        selector = str(
            rule.match.get("family_id")
            or rule.match.get("pattern_id")
            or rule.match.get("strategy")
            or "*"
        )
        signature = (
            rule.scope.get("function", "*"),
            str(rule.match.get("kind", "*")), selector, rule.priority,
        )
        if signature in signatures:
            raise ValueError(
                f"同作用域、类型和优先级存在歧义: {signature}"
            )
        signatures.add(signature)
    return pack


def load_rule_pack(path: Path | None) -> RulePack:
    if path is None:
        return BUILTIN_PACK
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = _pack_payload(raw)
    rules = tuple(_parse_rule(item) for item in payload.get("rules", []))
    project_pack = validate_pack(RulePack(
        name=str(payload.get("name", raw.get("name", Path(path).stem))),
        version=int(payload.get("version", raw.get("version", 1))), rules=rules,
        source=str(Path(path).resolve()),
    ))
    return merge_packs((BUILTIN_PACK, project_pack))


def rule_pack_from_mapping(raw: dict[str, Any], *, source: str = "manifest") -> RulePack:
    """Load an approved project rule pack already resolved by ProjectContext."""
    payload = raw.get("project_rule_pack", raw)
    if not isinstance(payload, dict):
        raise ValueError("ProjectRulePack 必须是 object")
    rules = tuple(_parse_rule(item) for item in payload.get("rules", []))
    project_pack = validate_pack(RulePack(
        name=str(payload.get("id", "project")),
        version=int(payload.get("version", 1)) if str(payload.get("version", "1")).isdigit() else 1,
        rules=rules, source=source,
    ))
    return merge_packs((BUILTIN_PACK, project_pack))


def merge_packs(packs: Iterable[RulePack]) -> RulePack:
    items = tuple(packs)
    return validate_pack(RulePack(
        name="+".join(pack.name for pack in items), version=1,
        rules=tuple(rule for pack in items for rule in pack.rules),
        source="+".join(pack.source for pack in items),
    ))


def review_rule_pack(path: Path) -> dict[str, Any]:
    """返回审批前审查摘要；不修改规则文件，也不改变候选状态。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = _pack_payload(raw)
    pack = load_rule_pack(Path(path))
    rules = list(payload.get("rules", []))
    return {
        "name": payload.get("name", raw.get("name", Path(path).stem)),
        "version": int(payload.get("version", raw.get("version", 1))),
        "source": str(Path(path).resolve()),
        "rules": [
            {
                "id": rule.rule_id,
                "status": rule.status,
                "scope": rule.scope,
                "kind": rule.match.get("kind"),
                "priority": rule.priority,
                "evidence_count": len(rule.evidence),
                "approval": bool(rule.approval),
            }
            for rule in pack.rules
            if rule.rule_id not in {item.rule_id for item in BUILTIN_PACK.rules}
        ],
        "counts": {
            "candidate": sum(item.get("status") == "candidate" for item in rules),
            "approved": sum(item.get("status") == "approved" for item in rules),
            "rejected": sum(item.get("status") == "rejected" for item in rules),
        },
    }


def approve_rule_pack(path: Path, output: Path, *, authority: str,
                      reason: str, rule_ids: set[str] | None = None) -> Path:
    """显式批准候选规则并写入新文件；原候选文件保持不变。"""
    if not authority.strip() or not reason.strip():
        raise ValueError("批准规则必须填写 authority 和 reason")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = _pack_payload(raw)
    changed = 0
    rules = []
    for item in payload.get("rules", []):
        copy = dict(item)
        if (copy.get("status") == "candidate"
                and (rule_ids is None or copy.get("id") in rule_ids)):
            copy["status"] = "approved"
            copy["approval"] = {"authority": authority, "reason": reason}
            changed += 1
        rules.append(copy)
    if changed == 0:
        raise ValueError("没有匹配的 candidate 规则可批准")
    result = dict(raw)
    if payload is raw:
        result["rules"] = rules
    else:
        result["candidate_pack"] = dict(payload)
        result["candidate_pack"]["rules"] = rules
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    validate_pack(load_rule_pack(output))
    return output
