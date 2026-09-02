"""Oracle construction and explicit external-callee environment."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ut_agent.baseline.model import TestBaseline
from ut_agent.ir import FunctionIR

from .evaluator import EvaluationResult, PASS


@dataclass(frozen=True)
class ExternalCall:
    callee: str
    call_orders: tuple[int, ...]
    return_used: bool
    pointer_outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExternalEnvironment:
    calls: tuple[ExternalCall, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"calls": [
            {
                "callee": call.callee,
                "call_orders": list(call.call_orders),
                "return_used": call.return_used,
                "pointer_outputs": list(call.pointer_outputs),
            }
            for call in self.calls
        ]}


@dataclass(frozen=True)
class OracleResult:
    status: str
    values: dict[str, Any]
    evidence: dict[str, str]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "values": self.values,
            "evidence": self.evidence,
            "errors": list(self.errors),
        }


def external_environment(ir: FunctionIR) -> ExternalEnvironment:
    grouped: dict[str, list[Any]] = {}
    for call in sorted(ir.calls, key=lambda item: item.order):
        if call.callee_kind == "memory_helper" or call.ptr_call:
            continue
        grouped.setdefault(call.callee, []).append(call)
    calls = []
    for callee in sorted(grouped):
        entries = grouped[callee]
        pointer_outputs = sorted({
            f"{callee}:{index}"
            for entry in entries
            for index, param in enumerate(entry.params)
            if param.is_ptr and param.is_written
        })
        calls.append(ExternalCall(
            callee=callee,
            call_orders=tuple(int(item.order) for item in entries),
            return_used=any(bool(item.return_used) for item in entries),
            pointer_outputs=tuple(pointer_outputs),
        ))
    return ExternalEnvironment(tuple(calls))


def build_oracle(ir: FunctionIR, evaluation: EvaluationResult,
                 baseline: TestBaseline) -> OracleResult:
    """Build an oracle from a completed semantic evaluation.

    The evaluator is the only producer of post-state values.  Keeping this
    function independent from the legacy engine prevents an oracle from
    silently recomputing or filling an expected value from a Golden row.
    ``ir`` is retained for API symmetry and for type-level call-site checks;
    required output names are carried by the evaluation result.
    """
    if baseline.status != "approved":
        return OracleResult("NEEDS_REVIEW", {}, {}, ("baseline is not approved",))
    if not isinstance(evaluation, EvaluationResult):
        return OracleResult(
            "NEEDS_REVIEW", {}, {},
            ("oracle requires SemanticEvaluator result",),
        )
    values = dict(evaluation.post_state or {})
    required = tuple(evaluation.required_outputs)
    if evaluation.status != PASS:
        return OracleResult(
            "NEEDS_REVIEW", values, {},
            (f"semantic evaluation {evaluation.status}: {evaluation.reason}",),
        )
    missing = tuple(name for name in required if name not in values)
    evidence = {
        name: "SemanticEvaluator post-state from FunctionIR effects"
        for name in values
    }
    if missing:
        return OracleResult("NEEDS_REVIEW", values, evidence,
                            tuple(f"缺少期望值 oracle: {name}" for name in missing))
    return OracleResult("VALIDATED", values, evidence)
