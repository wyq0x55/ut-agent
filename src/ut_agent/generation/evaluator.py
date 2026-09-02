"""Evaluate a solved obligation using only typed FunctionIR facts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ut_agent.ir import FunctionIR

from . import engine
from .model import TestObligation

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EvaluationResult:
    status: str
    obligation_id: str
    observed: Any = None
    checks: tuple[str, ...] = ()
    reason: str = ""
    pre_state: dict[str, Any] | None = None
    post_state: dict[str, Any] | None = None
    required_outputs: tuple[str, ...] = ()
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "obligation_id": self.obligation_id,
            "observed": self.observed,
            "checks": list(self.checks),
            "reason": self.reason,
            "pre_state": self.pre_state or {},
            "post_state": self.post_state or {},
            "required_outputs": list(self.required_outputs),
            "complete": self.complete,
        }


def evaluate_obligation(ir: FunctionIR, obligation: TestObligation,
                        assignment: dict[str, Any]) -> EvaluationResult:
    """Evaluate both the requested coverage fact and the resulting state.

    ``engine`` is used here only as the implementation of already extracted
    typed facts/effects.  No source text, Golden CSV, or renderer output is
    consulted.  A missing required effect is an incomplete semantic result,
    not a guessed oracle.
    """
    required = tuple(engine._required_outputs(ir))
    try:
        post_state = engine._generic_expected(ir, assignment)
    except (KeyError, TypeError, ValueError) as exc:
        return EvaluationResult(
            UNKNOWN, obligation.oid, reason=f"semantic state unavailable: {exc}",
            pre_state=dict(assignment), required_outputs=required,
        )

    def has_key(wanted: str) -> bool:
        return any(
            str(key) == wanted
            or str(key).lstrip("@*") == wanted.lstrip("@*")
            for key in post_state
        )

    complete = all(has_key(name) for name in required)
    if obligation.branch_id is None:
        if not complete:
            missing = ", ".join(name for name in required if not has_key(name))
            return EvaluationResult(
                UNKNOWN, obligation.oid, True, ("entry",),
                f"semantic evaluation incomplete: {missing}",
                dict(assignment), post_state, required, False,
            )
        return EvaluationResult(
            PASS, obligation.oid, True, ("entry", "post-state"), "",
            dict(assignment), post_state, required, True,
        )
    branch = next((item for item in ir.branches
                   if item.bid == obligation.branch_id), None)
    if branch is None:
        return EvaluationResult(UNKNOWN, obligation.oid,
                                reason=f"unknown branch: {obligation.branch_id}",
                                pre_state=dict(assignment), post_state=post_state,
                                required_outputs=required, complete=complete)
    try:
        env = engine._control_env(assignment, ir)
        if obligation.kind == "case":
            case = engine._find_switch_case(branch, obligation)
            if case is None:
                return EvaluationResult(UNKNOWN, obligation.oid,
                                        reason="unknown switch case",
                                        pre_state=dict(assignment),
                                        post_state=post_state,
                                        required_outputs=required,
                                        complete=complete)
            actual = engine._switch_selector_value(branch, ir, env)
            observed = engine._switch_case_matches(case, actual, branch.cases)
            expected = True
        elif obligation.kind == "mcdc":
            index = obligation.condition_index
            if index is None or index >= len(branch.atoms):
                return EvaluationResult(UNKNOWN, obligation.oid,
                                        reason="missing MC/DC condition index",
                                        pre_state=dict(assignment),
                                        post_state=post_state,
                                        required_outputs=required,
                                        complete=complete)
            actual_values = [engine.evaluate_atom(atom, env)
                             for atom in branch.atoms]
            observed = {
                "condition": actual_values[index],
                "decision": engine.evaluate_branch(branch, env, post_state),
            }
            expected = {
                "condition": obligation.outcome,
                "decision": obligation.outcome,
            }
        elif obligation.kind == "condition":
            index = obligation.condition_index
            if index is None or index >= len(branch.atoms):
                return EvaluationResult(UNKNOWN, obligation.oid,
                                        reason="missing condition index",
                                        pre_state=dict(assignment),
                                        post_state=post_state,
                                        required_outputs=required,
                                        complete=complete)
            observed = engine.evaluate_atom(branch.atoms[index], env)
            expected = obligation.outcome
        elif obligation.kind == "boundary":
            index = obligation.condition_index
            if index is None or index >= len(branch.atoms):
                return EvaluationResult(UNKNOWN, obligation.oid,
                                        reason="missing boundary condition index",
                                        pre_state=dict(assignment),
                                        post_state=post_state,
                                        required_outputs=required,
                                        complete=complete)
            observed = engine._lookup(env, branch.atoms[index].var)
            expected = obligation.boundary_value
        else:
            observed = engine.evaluate_branch(branch, env, post_state)
            expected = obligation.outcome
    except (KeyError, TypeError, ValueError) as exc:
        return EvaluationResult(UNKNOWN, obligation.oid, reason=str(exc),
                                pre_state=dict(assignment), post_state=post_state,
                                required_outputs=required, complete=complete)
    status = PASS if observed == expected else FAIL
    if status == PASS and not complete:
        missing = ", ".join(name for name in required if not has_key(name))
        return EvaluationResult(
            UNKNOWN, obligation.oid, observed, ("branch-outcome",),
            f"semantic evaluation incomplete: {missing}",
            dict(assignment), post_state, required, False,
        )
    return EvaluationResult(status, obligation.oid, observed,
                             ("branch-outcome", "post-state"),
                             "" if status == PASS else f"expected {expected}",
                             dict(assignment), post_state, required, complete)
