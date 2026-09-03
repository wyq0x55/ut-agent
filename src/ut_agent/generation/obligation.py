"""Derive executable coverage obligations from typed FunctionIR facts."""
from __future__ import annotations

from dataclasses import replace

from ut_agent.baseline.model import TestBaseline
from ut_agent.ir import FunctionIR

from .boundary import typed_boundary_points
from .model import TestObligation


def _case_label(case) -> str:
    if case.is_default:
        return "default:"
    if case.value is not None:
        return f"case {case.value}:"
    return f"case {case.label}:"


def _mcdc_enabled(baseline: TestBaseline) -> bool:
    if baseline.mcdc_enabled is not None:
        return baseline.mcdc_enabled
    return bool(baseline.coverage.get("mcdc", False)
                or baseline.condition_policy.get("mcdc", False))


def _policy_bool(section: dict, names: tuple[str, ...], default: bool) -> bool:
    for name in names:
        if name in section:
            return bool(section[name])
    return default


def _obligation(baseline: TestBaseline, *, source_fact: str,
                rule_id: str, project_rule_ref: str = "", **kwargs) -> TestObligation:
    return TestObligation(
        baseline_ref=baseline.ref, rule_id=rule_id, source_fact=source_fact,
        project_rule_ref=project_rule_ref,
        **kwargs,
    )


def derive_obligations(ir: FunctionIR, baseline: TestBaseline,
                       *, mcdc_enabled: bool | None = None,
                       project_rule_pack: dict | None = None) -> tuple[TestObligation, ...]:
    """Create a stable obligation list without inspecting source text.

    MC/DC obligations are emitted as two members of one pair.  For a pure
    ``&&`` or ``||`` condition, the other conditions are fixed to the
    independence-preserving value by the solver.  Mixed condition trees are
    intentionally left to ordinary branch obligations; their independence
    proof requires extractor metadata not represented by the current IR.
    """
    if baseline.status != "approved":
        raise ValueError(f"只能从 approved TestBaseline 生成: {baseline.ref}")
    obligations: list[TestObligation] = []
    project_rule_ref = ""
    if isinstance(project_rule_pack, dict):
        payload = project_rule_pack.get("project_rule_pack", project_rule_pack)
        if isinstance(payload, dict):
            pack_id = payload.get("id")
            pack_version = payload.get("version")
            if pack_id and pack_version is not None:
                project_rule_ref = f"{pack_id}@{pack_version}"
    branch_enabled = _policy_bool(
        baseline.coverage, ("branch_outcome", "branch"), True,
    )
    condition_enabled = _policy_bool(
        baseline.condition_policy, ("condition_outcome", "condition"), False,
    )
    boundary_enabled = _policy_bool(
        baseline.boundary_policy, ("obligations", "points"), False,
    )
    loop_enabled = _policy_bool(
        baseline.loop_policy, ("iteration_count", "boundary_state"), False,
    )
    switch_enabled = _policy_bool(
        baseline.switch_policy, ("preserve_cases", "cases"), True,
    )
    for branch in ir.branches:
        if branch.kind == "for":
            if loop_enabled:
                rule_name = (
                    "iteration_count"
                    if baseline.loop_policy.get("iteration_count", False)
                    else "boundary_state"
                )
                obligations.append(_obligation(
                    baseline, source_fact=f"branch:{branch.bid}",
                    rule_id=f"baseline.loop_policy.{rule_name}",
                    oid=f"{branch.bid}:loop-entry", kind="loop",
                    branch_id=branch.bid, outcome=True,
                    boundary_class="loop-entry",
                    description=branch.cond_text or branch.cond_text_expanded,
                ))
            continue
        if branch.kind == "switch" and branch.cases and switch_enabled:
            for index, case in enumerate(branch.cases):
                label = _case_label(case)
                obligations.append(_obligation(
                    baseline, source_fact=f"branch:{branch.bid}",
                    rule_id="baseline.switch_policy.preserve_cases",
                    oid=f"{branch.bid}:case:{index}", kind="case",
                    branch_id=branch.bid, description=label, case_label=label,
                ))
            continue
        if branch.kind == "switch" and branch.cases:
            continue
        if branch_enabled:
            outcomes = ((branch.constant_value,) if branch.constant_value is not None
                        else (True, False))
            for outcome in outcomes:
                obligations.append(_obligation(
                    baseline, source_fact=f"branch:{branch.bid}",
                    rule_id="baseline.coverage.branch_outcome",
                    oid=f"{branch.bid}:{'T' if outcome else 'F'}", kind="branch",
                    branch_id=branch.bid, outcome=outcome,
                    description=branch.cond_text or branch.cond_text_expanded,
                ))
        if condition_enabled:
            for index, _atom in enumerate(branch.atoms):
                for desired in (True, False):
                    obligations.append(_obligation(
                        baseline, source_fact=f"branch:{branch.bid}:atom:{index}",
                        rule_id="baseline.condition_policy.condition_outcome",
                        oid=f"{branch.bid}:condition:{index}:"
                            f"{'T' if desired else 'F'}",
                        kind="condition", branch_id=branch.bid,
                        outcome=desired, condition_index=index,
                    ))
        if boundary_enabled:
            for index, atom in enumerate(branch.atoms):
                type_info = atom.type_info
                if type_info is None:
                    control = next((item for item in ir.control_vars
                                    if item.var == atom.var or item.name == atom.var), None)
                    type_info = control.type_info if control else None
                for point in typed_boundary_points(atom.boundary, type_info):
                    label = "exact" if point == atom.boundary else (
                        "below" if point < atom.boundary else "above"
                    )
                    obligations.append(_obligation(
                        baseline, source_fact=f"branch:{branch.bid}:atom:{index}",
                        rule_id="baseline.boundary_policy.points",
                        oid=f"{branch.bid}:boundary:{index}:{label}:{point}",
                        kind="boundary", branch_id=branch.bid,
                        boundary_class=label, condition_index=index,
                        boundary_value=point,
                        description=f"atom {index} {label} boundary {point}",
                    ))
        enabled = _mcdc_enabled(baseline) if mcdc_enabled is None else bool(mcdc_enabled)
        if not enabled or len(branch.atoms) < 2:
            continue
        if (branch.connective or "") not in {"&&", "||"}:
            continue
        for index, atom in enumerate(branch.atoms):
            pair_id = f"{branch.bid}:mcdc:{index}"
            for desired in (True, False):
                obligations.append(_obligation(
                    baseline, source_fact=f"branch:{branch.bid}:atom:{index}",
                    rule_id="baseline.condition_policy.mcdc",
                    oid=f"{pair_id}:{'T' if desired else 'F'}",
                    kind="mcdc", branch_id=branch.bid,
                    outcome=desired, description=f"MC/DC atom {index}={'T' if desired else 'F'}",
                    condition_index=index, pair_id=pair_id,
                ))
    if not obligations:
        obligations.append(_obligation(
            baseline, source_fact=f"function:{ir.name}",
            rule_id="baseline.coverage.function_entry",
            oid="ENTRY", kind="execution", description="function entry",
        ))
    if project_rule_ref:
        obligations = [replace(item, project_rule_ref=project_rule_ref)
                       for item in obligations]
    return tuple(obligations)
