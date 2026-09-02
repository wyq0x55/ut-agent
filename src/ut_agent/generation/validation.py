"""Generation and target-entry validation gates."""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from typing import Iterable

from ut_agent.baseline.model import TestBaseline
from ut_agent.ir import FunctionIR

from . import engine
from .evaluator import EvaluationResult, PASS
from .model import NEEDS_REVIEW, TestIntent, ValidationResult, VALIDATED
from .oracle import OracleResult


@dataclass(frozen=True)
class SuiteValidation:
    status: str
    checks: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def validate_mcdc_pairs(ir: FunctionIR,
                        intents: Iterable[TestIntent]) -> tuple[str, ...]:
    """Re-prove independence for every emitted MC/DC pair.

    Per-member branch checks are insufficient: two rows could both have the
    requested atom value while changing a sibling or leaving the decision
    unchanged.  This check compares the actual typed atom/decision values.
    """
    grouped: dict[str, list[TestIntent]] = defaultdict(list)
    for intent in intents:
        pair_id = intent.obligation.pair_id
        if pair_id:
            grouped[pair_id].append(intent)
    errors: list[str] = []
    for pair_id in sorted(grouped):
        members = grouped[pair_id]
        if len(members) != 2:
            errors.append(f"MC/DC pair {pair_id} 必须恰好包含两个 witness")
            continue
        first, second = members
        if {first.obligation.outcome, second.obligation.outcome} != {True, False}:
            errors.append(f"MC/DC pair {pair_id} 未覆盖 selected atom 的 T/F")
            continue
        if first.obligation.condition_index != second.obligation.condition_index:
            errors.append(f"MC/DC pair {pair_id} condition index 不一致")
            continue
        branch = next((item for item in ir.branches
                       if item.bid == first.obligation.branch_id), None)
        index = first.obligation.condition_index
        if branch is None or index is None or index >= len(branch.atoms):
            errors.append(f"MC/DC pair {pair_id} 缺少 typed branch/atom")
            continue
        try:
            envs = [engine._control_env(item.inputs, ir)
                    for item in (first, second)]
            atom_values = [
                [engine.evaluate_atom(atom, env) for atom in branch.atoms]
                for env in envs
            ]
            decisions = [engine.evaluate_branch(branch, env) for env in envs]
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"MC/DC pair {pair_id} 不可证明: {exc}")
            continue
        if atom_values[0][index] == atom_values[1][index]:
            errors.append(f"MC/DC pair {pair_id} selected atom 未变化")
        if (atom_values[0][:index] + atom_values[0][index + 1:]
                != atom_values[1][:index] + atom_values[1][index + 1:]):
            errors.append(f"MC/DC pair {pair_id} sibling atoms 未固定")
        if decisions[0] == decisions[1]:
            errors.append(f"MC/DC pair {pair_id} decision 未变化")
    return tuple(errors)


def validate_intent_contract(ir: FunctionIR, intent: TestIntent,
                             baseline: TestBaseline,
                             *, evaluation: EvaluationResult | None = None,
                             oracle: OracleResult | None = None) -> ValidationResult:
    errors: list[str] = []
    checks: list[str] = ["baseline-approved"]
    if baseline.status != "approved":
        errors.append(f"TestBaseline 未审批: {baseline.ref}")
    result = engine.validate_intent(ir, intent, evaluation=evaluation)
    checks.extend(result.checks)
    errors.extend(result.errors)
    if evaluation is not None:
        if evaluation.status != PASS:
            errors.append(f"obligation evaluation {evaluation.status}: {evaluation.reason}")
        elif not evaluation.complete:
            errors.append("semantic evaluation incomplete")
        else:
            checks.append("obligation-evaluated")
    if oracle is not None:
        if oracle.status != VALIDATED:
            errors.extend(oracle.errors)
        else:
            checks.append("oracle-proven")
    status = VALIDATED if not errors else NEEDS_REVIEW
    return ValidationResult(status, tuple(dict.fromkeys(checks)),
                            tuple(dict.fromkeys(errors)))


def validate_suite(intents: Iterable[TestIntent], *, unresolved: Iterable[str] = (),
                   baseline: TestBaseline | None = None,
                   ir: FunctionIR | None = None) -> SuiteValidation:
    errors = list(unresolved)
    checks: list[str] = []
    if baseline is not None:
        if baseline.status != "approved":
            errors.append(f"TestBaseline 未审批: {baseline.ref}")
        else:
            checks.append("baseline-approved")
    items = tuple(intents)
    if not items:
        errors.append("没有生成 TestIntent")
    for intent in items:
        if intent.validation.valid:
            checks.append(f"intent:{intent.case_id}")
        else:
            errors.extend(intent.validation.errors)
    if ir is not None:
        pair_errors = validate_mcdc_pairs(ir, items)
        errors.extend(pair_errors)
        if pair_errors:
            checks.append("mcdc-independence-failed")
        elif any(item.obligation.pair_id for item in items):
            checks.append("mcdc-independence")
    return SuiteValidation(
        VALIDATED if items and not errors else NEEDS_REVIEW,
        tuple(checks), tuple(dict.fromkeys(errors)),
    )


__all__ = [
    "SuiteValidation", "validate_intent_contract", "validate_mcdc_pairs",
    "validate_suite",
]
