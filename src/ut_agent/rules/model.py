"""规则引擎的语义模型。

这些 dataclass 是 FunctionIR 与目标输出适配器之间的确定性契约。生成器只把
``VALIDATED`` 的 TestIntent 交给渲染层；不能证明的用例保留在 manifest 中。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


VALIDATED = "VALIDATED"
NEEDS_REVIEW = "NEEDS_REVIEW"
UNSUPPORTED = "UNSUPPORTED"
EXECUTION_FAILED = "EXECUTION_FAILED"


@dataclass(frozen=True)
class Constraint:
    kind: str
    variable: str
    operator: str
    value: Any
    evidence: str = ""


@dataclass(frozen=True)
class TestObligation:
    oid: str
    kind: str
    branch_id: str | None = None
    outcome: bool | None = None
    boundary_class: str | None = None
    description: str = ""
    case_label: str | None = None


@dataclass(frozen=True)
class RuleTrace:
    rule_id: str
    evidence: str
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    status: str
    checks: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status == VALIDATED and not self.errors


@dataclass(frozen=True)
class TestIntent:
    case_id: str
    obligation: TestObligation
    inputs: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    raw_inputs: dict[str, str] = field(default_factory=dict)
    raw_expected: dict[str, str] = field(default_factory=dict)
    stub_behavior: dict[str, Any] = field(default_factory=dict)
    constraints: tuple[Constraint, ...] = ()
    trace: tuple[RuleTrace, ...] = ()
    validation: ValidationResult = field(
        default_factory=lambda: ValidationResult(NEEDS_REVIEW)
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GenerationResult:
    function: str
    status: str
    intents: tuple[TestIntent, ...] = ()
    issues: tuple[str, ...] = ()
    rule_pack: str = "builtin"

    @property
    def validated_intents(self) -> tuple[TestIntent, ...]:
        return tuple(item for item in self.intents if item.validation.valid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "function": self.function,
            "status": self.status,
            "rule_pack": self.rule_pack,
            "issues": list(self.issues),
            "intents": [item.to_dict() for item in self.intents],
        }
