"""Typed versioned Base TestBaseline contract.

Project-level switches such as MC/DC and approved exceptions belong to the
project manifest.  Keeping them out of this model makes the baseline a stable
base-test contract instead of a second project-policy registry.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


_POLICY_NAMES = (
    "coverage", "condition_policy", "boundary_policy", "switch_policy",
    "loop_policy", "array_policy", "stub_policy", "ordering_policy",
)


@dataclass(frozen=True)
class TestBaseline:
    id: str
    version: str
    status: str
    approval: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    rules: tuple[dict[str, Any], ...] = ()
    coverage: dict[str, Any] = field(default_factory=dict)
    condition_policy: dict[str, Any] = field(default_factory=dict)
    boundary_policy: dict[str, Any] = field(default_factory=dict)
    switch_policy: dict[str, Any] = field(default_factory=dict)
    loop_policy: dict[str, Any] = field(default_factory=dict)
    array_policy: dict[str, Any] = field(default_factory=dict)
    stub_policy: dict[str, Any] = field(default_factory=dict)
    ordering_policy: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TestBaseline":
        raw = value.get("baseline", value)
        if not isinstance(raw, Mapping):
            raise ValueError("baseline 必须是 object")
        missing = {"id", "version", "status"} - set(raw)
        if missing:
            raise ValueError(f"TestBaseline 缺少字段: {sorted(missing)}")
        approval = raw.get("approval", {})
        if not isinstance(approval, Mapping):
            raise ValueError("TestBaseline.approval 必须是 object")
        status = str(raw["status"])
        if status == "approved" and not approval:
            raise ValueError("approved TestBaseline requires approval metadata")
        policies = {
            name: dict(raw.get(name, {}))
            for name in _POLICY_NAMES
        }
        if any(not isinstance(raw.get(name, {}), Mapping) for name in _POLICY_NAMES):
            raise ValueError("TestBaseline policy section 必须是 object")
        raw_rules = raw.get("rules", [])
        if not isinstance(raw_rules, list) or any(
            not isinstance(item, Mapping) for item in raw_rules
        ):
            raise ValueError("TestBaseline.rules 必须是 object array")
        return cls(
            id=str(raw["id"]), version=str(raw["version"]),
            status=status,
            approval=dict(approval),
            source=dict(raw.get("source", {})),
            rules=tuple(dict(item) for item in raw_rules),
            **policies,
        )

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rules"] = list(self.rules)
        return {"baseline": data}
