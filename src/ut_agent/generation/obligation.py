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
    # These are the executable fields of the approved baseline contract.
    # Keep the rule IDs tied to the approved source mapping instead of a
    # Python implementation path such as ``baseline.boundary_policy.points``.
    branch_enabled = bool(baseline.coverage.get("branch_outcome", False))
    condition_enabled = bool(
        baseline.condition_policy.get("condition_outcome", False)
    )
    logical_connectives = set(
        str(item) for item in baseline.condition_policy.get(
            "logical_connectives", ()
        )
    )
    boundary_enabled = bool(baseline.boundary_policy.get("typed", False))
    loop_enabled = bool(
        baseline.loop_policy.get("iteration_count", False)
        or baseline.loop_policy.get("boundary_state", False)
    )
    switch_enabled = bool(baseline.switch_policy.get("preserve_cases", False))
    include_default = bool(baseline.switch_policy.get("include_default", False))
    for branch in ir.branches:
        if branch.kind == "for":
            if loop_enabled:
                obligations.append(_obligation(
                    baseline, source_fact=f"branch:{branch.bid}",
                    rule_id="psd.6.control",
                    oid=f"{branch.bid}:loop-entry", kind="loop",
                    branch_id=branch.bid, outcome=True,
                    boundary_class="loop-entry",
                    description=branch.cond_text or branch.cond_text_expanded,
                ))
            continue
        if branch.kind == "switch" and branch.cases and switch_enabled:
            for index, case in enumerate(branch.cases):
                if case.is_default and not include_default:
                    continue
                label = _case_label(case)
                obligations.append(_obligation(
                    baseline, source_fact=f"branch:{branch.bid}",
                    rule_id="psd.6.control",
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
                    rule_id="psd.4.compare",
                    oid=f"{branch.bid}:{'T' if outcome else 'F'}", kind="branch",
                    branch_id=branch.bid, outcome=outcome,
                    description=branch.cond_text or branch.cond_text_expanded,
                ))
        condition_branch_enabled = condition_enabled and (
            len(branch.atoms) <= 1
            or not logical_connectives
            or (branch.connective or "") in logical_connectives
        )
        if condition_branch_enabled:
            for index, _atom in enumerate(branch.atoms):
                for desired in (True, False):
                    obligations.append(_obligation(
                        baseline, source_fact=f"branch:{branch.bid}:atom:{index}",
                        rule_id="psd.4.compare",
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
                for point in typed_boundary_points(
                        atom.boundary, type_info, baseline.boundary_policy):
                    label = "exact" if point == atom.boundary else (
                        "below" if point < atom.boundary else "above"
                    )
                    obligations.append(_obligation(
                        baseline, source_fact=f"branch:{branch.bid}:atom:{index}",
                        rule_id="psd.4.compare",
                        oid=f"{branch.bid}:boundary:{index}:{label}:{point}",
                        kind="boundary", branch_id=branch.bid,
                        boundary_class=label, condition_index=index,
                        boundary_value=point,
                        description=f"atom {index} {label} boundary {point}",
                    ))
        # MC/DC is a project-level switch.  A direct baseline API call has no
        # project context, so it is deliberately disabled unless the caller
        # supplies the explicit switch.
        enabled = bool(mcdc_enabled)
        if (not enabled or len(branch.atoms) < 2
                or (logical_connectives
                    and (branch.connective or "") not in logical_connectives)):
            continue
        if (branch.connective or "") not in {"&&", "||"}:
            continue
        for index, atom in enumerate(branch.atoms):
            pair_id = f"{branch.bid}:mcdc:{index}"
            for desired in (True, False):
                obligations.append(_obligation(
                    baseline, source_fact=f"branch:{branch.bid}:atom:{index}",
                    rule_id="psd.4.mcdc",
                    oid=f"{pair_id}:{'T' if desired else 'F'}",
                    kind="mcdc", branch_id=branch.bid,
                    outcome=desired, description=f"MC/DC atom {index}={'T' if desired else 'F'}",
                    condition_index=index, pair_id=pair_id,
                ))
    if not obligations:
        obligations.append(_obligation(
            baseline, source_fact=f"function:{ir.name}",
            rule_id="psd.6.control",
            oid="ENTRY", kind="execution", description="function entry",
        ))
    if project_rule_ref:
        obligations = [replace(item, project_rule_ref=project_rule_ref)
                       for item in obligations]
    if mcdc_enabled is not None:
        obligations = [replace(
            item, project_mcdc_enabled=bool(mcdc_enabled)
        ) for item in obligations]
    return tuple(obligations)
