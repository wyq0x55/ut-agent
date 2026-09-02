"""Typed TestBaseline contract.

The baseline is a project-level, versioned input to generation.  Policy
sections intentionally remain data, rather than executable Python, so loading
one cannot introduce a second rule language or an implicit version upgrade.
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
    base_profile: str = ""
    mcdc_enabled: bool | None = None
    approved_exceptions: tuple[str, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)
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
        policies = {
            name: dict(raw.get(name, {}))
            for name in _POLICY_NAMES
        }
        if any(not isinstance(raw.get(name, {}), Mapping) for name in _POLICY_NAMES):
            raise ValueError("TestBaseline policy section 必须是 object")
        return cls(
            id=str(raw["id"]), version=str(raw["version"]),
            status=str(raw["status"]),
            base_profile=str(raw.get("base_profile", "")),
            mcdc_enabled=(bool(raw["mcdc_enabled"])
                          if "mcdc_enabled" in raw else None),
            approved_exceptions=tuple(str(item) for item in
                                      raw.get("approved_exceptions", [])),
            source=dict(raw.get("source", {})),
            **policies,
        )

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["approved_exceptions"] = list(self.approved_exceptions)
        return {"baseline": data}
