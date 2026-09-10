"""Finite deterministic solver for typed coverage obligations."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import chain, product
from typing import Any

from ut_agent.baseline.model import TestBaseline
from ut_agent.ir import FunctionIR

from . import engine
from .constraint import ConstraintSet, constraints_for
from .model import TestObligation

SAT = "SAT"
UNSAT = "UNSAT"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SolveResult:
    status: str
    obligation: TestObligation
    assignment: dict[str, Any] | None = None
    constraints: ConstraintSet | None = None
    checked: int = 0
    reason: str = ""

    @property
    def satisfied_constraints(self) -> ConstraintSet | None:
        """Compatibility-neutral name used by the formal solver contract."""
        return self.constraints

    @property
    def solver_trace(self) -> tuple[str, ...]:
        trace = ["finite-typed-domain", f"checked:{self.checked}"]
        if self.reason:
            trace.append(self.reason)
        return tuple(trace)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "obligation": self.obligation.to_dict()
            if hasattr(self.obligation, "to_dict") else self.obligation.__dict__,
            "assignment": self.assignment,
            "constraints": (self.constraints.to_dict()
                            if self.constraints else None),
            "checked": self.checked,
            "reason": self.reason,
            "satisfied_constraints": (
                self.satisfied_constraints.to_dict()
                if self.satisfied_constraints else None
            ),
            "solver_trace": list(self.solver_trace),
        }


def _matches(ir: FunctionIR, obligation: TestObligation,
             env: dict[str, Any]) -> bool | None:
    if obligation.branch_id is None:
        return True
    branch = next((item for item in ir.branches
                   if item.bid == obligation.branch_id), None)
    if branch is None:
        return None
    path = engine.branch_path_reachable(ir, branch, env)
    if path is not True:
        return path
    if obligation.kind == "case":
        selector = engine._switch_selector_value(branch, ir, env)
        return engine._switch_obligation_matches(branch, obligation, selector)
    if obligation.kind == "mcdc":
        index = obligation.condition_index
        if index is None or index < 0 or index >= len(branch.atoms):
            return None
        expected_truths = engine._mcdc_expected_truths(
            branch, index, bool(obligation.outcome)
        )
        if expected_truths is None:
            return None
        atom_values = [engine.evaluate_atom(atom, env) for atom in branch.atoms]
        if any(atom_values[pos] != expected
               for pos, expected in expected_truths.items()):
            return False
        return engine.evaluate_branch(branch, env) == obligation.outcome
    if obligation.kind == "condition":
        index = obligation.condition_index
        if index is None or index < 0 or index >= len(branch.atoms):
            return None
        return engine.evaluate_atom(branch.atoms[index], env) == obligation.outcome
    if obligation.kind == "boundary":
        index = obligation.condition_index
        if index is None or index < 0 or index >= len(branch.atoms):
            return None
        try:
            return engine._lookup(env, branch.atoms[index].var) == obligation.boundary_value
        except KeyError:
            return None
    return engine.evaluate_branch(branch, env) == obligation.outcome


def solve_obligation(ir: FunctionIR, obligation: TestObligation,
                     baseline: TestBaseline) -> SolveResult:
    """Search only extractor-proven finite domains.

    ``UNKNOWN`` means the available typed facts cannot establish a finite
    search or a constructible proof.  It is never converted to a guessed
    input vector or a false branch result.
    """
    if baseline.status != "approved":
        return SolveResult(UNKNOWN, obligation, reason="baseline is not approved")
    try:
        domains, fixed = engine._generic_inputs(ir, baseline)
    except (KeyError, TypeError, ValueError) as exc:
        return SolveResult(UNKNOWN, obligation, reason=str(exc))
    # Solver cardinality is an engine safety guard, not a second baseline
    # strategy field.  The semantic policy controls which typed candidates
    # enter this finite search; the guard remains fixed and deterministic.
    limit = 4096
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        return SolveResult(UNKNOWN, obligation, reason="invalid solver limit")
    keys = sorted(domains)
    cardinality = 1
    for key in keys:
        cardinality *= len(domains[key])
    full_candidates = (
        engine._control_env({**fixed, **dict(zip(keys, combo))}, ir)
        for combo in product(*(domains[key] for key in keys))
    )
    if ir.branches:
        targeted = engine._targeted_generic_candidates(
            ir, domains, fixed, obligation
        )
        first = next(targeted, None)
        if first is not None:
            candidates = chain((first,), targeted, () if cardinality > limit else full_candidates)
        elif cardinality > limit:
            candidates = ()
        else:
            candidates = full_candidates
    else:
        candidates = full_candidates
    checked = 0
    saw_unknown = False
    for env in candidates:
        checked += 1
        try:
            match = _matches(ir, obligation, env)
        except (KeyError, TypeError, ValueError):
            match = None
        if match is True:
            constraints = constraints_for(ir, obligation, env)
            return SolveResult(SAT, obligation, dict(env), constraints, checked)
        if match is None:
            saw_unknown = True
        if checked >= limit and cardinality > limit:
            break
    if saw_unknown:
        return SolveResult(UNKNOWN, obligation, checked=checked,
                           reason="obligation cannot be proven from FunctionIR")
    return SolveResult(UNSAT, obligation, checked=checked,
                       reason="finite typed domain has no satisfying assignment")
