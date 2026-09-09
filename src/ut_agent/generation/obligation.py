"""Derive executable coverage obligations from typed FunctionIR facts."""
from __future__ import annotations

from dataclasses import replace

from ut_agent.baseline.model import TestBaseline
from ut_agent.ir import FunctionIR

from .boundary import (
    switch_default_points, typed_boundary_points, typed_status_points,
)
from .model import TestObligation
from .semantic import index_driver_limit


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


def _normalized(value: str) -> str:
    return "".join(str(value or "").split())


def _index_limit_for_atom(ir: FunctionIR, atom) -> int | None:
    """Return a proven bound when an atom observes an indexed object.

    ``index_drivers`` comes from the extractor's access relation.  Matching
    the driver against the serialized semantic path only locates that typed
    relation; it does not infer C syntax or array shape in Python.
    """
    atom_path = _normalized(getattr(atom, "var", ""))
    limits: list[int] = []
    for raw in ir.global_objects:
        for driver in getattr(raw, "index_drivers", []):
            normalized = _normalized(driver)
            if not normalized or normalized not in atom_path:
                continue
            limit = index_driver_limit(ir, driver)
            if limit is not None:
                limits.append(limit)
    return min(limits) if limits else None


def _index_limit_for_selector(ir: FunctionIR, selector: str) -> int | None:
    wanted = _normalized(selector)
    limits: list[int] = []
    for raw in ir.global_objects:
        for driver in getattr(raw, "index_drivers", []):
            if _normalized(driver) != wanted:
                continue
            limit = index_driver_limit(ir, driver)
            if limit is not None:
                limits.append(limit)
    return min(limits) if limits else None


def _boundary_conflicts_with_parent(ir: FunctionIR, branch, atom, point) -> bool:
    """Reject a point contradicted by a single-atom parent path fact."""
    by_id = {item.bid: item for item in ir.branches}
    current = branch
    visited: set[str] = set()
    while current.parent_bid:
        if current.bid in visited:
            return False
        visited.add(current.bid)
        parent = by_id.get(current.parent_bid)
        if parent is None:
            return False
        if parent.kind != "switch" and len(parent.atoms) == 1:
            required = current.parent_outcome
            if required is None:
                required = not (
                    current.kind == "elseif" and current.chain_index > 0
                )
            parent_atom = parent.atoms[0]
            parent_control = next(
                (item for item in ir.control_vars
                 if item.var == parent_atom.var or item.name == parent_atom.var),
                None,
            )
            parent_value = None
            if (parent_control is not None
                    and parent_control.value_origin is not None
                    and parent_control.value_origin.kind == "const_table_field"):
                origin = parent_control.value_origin
                driver = _normalized(origin.driver or "")
                if driver == _normalized(atom.var):
                    try:
                        parent_value = origin.table_values.get(str(int(point)))
                    except (TypeError, ValueError):
                        parent_value = None
                    try:
                        if parent_value is not None:
                            parent_value = int(parent_value)
                    except (TypeError, ValueError):
                        parent_value = None
            if (_normalized(parent_atom.var) == _normalized(atom.var)
                    and parent_atom.boundary is not None
                    and parent_atom.op in {"==", "!="}
                    and atom.op in {"==", "!="}):
                parent_truth = (
                    point == parent_atom.boundary
                    if parent_atom.op == "=="
                    else point != parent_atom.boundary
                )
                if bool(parent_truth) != bool(required):
                    return True
            elif (parent_value is not None
                  and parent_atom.boundary is not None
                  and parent_atom.op in {"==", "!="}):
                parent_truth = (
                    parent_value == parent_atom.boundary
                    if parent_atom.op == "=="
                    else parent_value != parent_atom.boundary
                )
                if bool(parent_truth) != bool(required):
                    return True
        current = parent
    return False


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
            selector = branch.selector
            selector_names = {
                name for name in (
                    selector.driver if selector else None,
                    selector.expression if selector else None,
                ) if name
            }
            control = next(
                (item for item in ir.control_vars
                 if item.name in selector_names
                 or item.var in selector_names),
                None,
            )
            default_points = (
                switch_default_points(
                    branch.cases, control.type_info,
                    baseline.boundary_policy,
                )
                if control is not None else ()
            )
            selector_expression = (
                selector.driver if selector else None
            ) or (selector.expression if selector else "")
            selector_limit = _index_limit_for_selector(
                ir, selector_expression,
            ) if selector_expression else None
            if selector_limit is not None:
                default_points = tuple(
                    point for point in default_points
                    if isinstance(point, int) and 0 <= point < selector_limit
                )
                if selector_limit > 0:
                    explicit_values = {
                        case.value for case in branch.cases
                        if not case.is_default and case.value is not None
                    }
                    if selector_limit - 1 not in explicit_values:
                        default_points = tuple(sorted(
                            {*default_points, selector_limit - 1}
                        ))
            default_point = default_points[0] if default_points else None
            has_default = any(case.is_default for case in branch.cases)
            for index, case in enumerate(branch.cases):
                if case.is_default and not include_default:
                    continue
                label = _case_label(case)
                obligations.append(_obligation(
                    baseline, source_fact=f"branch:{branch.bid}",
                    rule_id="psd.6.control",
                    oid=f"{branch.bid}:case:{index}", kind="case",
                    branch_id=branch.bid, description=label, case_label=label,
                    boundary_value=(default_point
                                    if case.is_default else None),
                ))
            if include_default and has_default and control is not None:
                for point_index, point in enumerate(default_points[1:], 1):
                    default_label = next(
                        (_case_label(case) for case in branch.cases
                         if case.is_default),
                        "default:",
                    )
                    obligations.append(_obligation(
                        baseline, source_fact=f"branch:{branch.bid}:selector",
                        rule_id="psd.6.control",
                        oid=f"{branch.bid}:default:{point_index}:{point}",
                        kind="case", branch_id=branch.bid,
                        description=f"組合せ(default:{point_index})",
                        case_label=default_label,
                        boundary_class="default",
                        boundary_value=point,
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
                control = next((item for item in ir.control_vars
                                if item.var == atom.var or item.name == atom.var), None)
                type_info = atom.type_info
                if type_info is None:
                    type_info = control.type_info if control else None
                if control is not None and control.source == "stub":
                    # Stub return codes are categorical executable values,
                    # even when their ABI type is an unsigned byte.  Keep
                    # obligation derivation aligned with control_candidates.
                    points = typed_status_points(
                        atom.boundary, type_info, baseline.boundary_policy
                    )
                else:
                    points = typed_boundary_points(
                        atom.boundary, type_info, baseline.boundary_policy
                    )
                index_limit = _index_limit_for_atom(ir, atom)
                if index_limit is not None:
                    points = tuple(
                        point for point in points
                        if isinstance(point, int) and 0 <= point < index_limit
                    )
                if control is not None:
                    origin = control.value_origin
                    if (origin is not None
                            and origin.kind == "const_table_field"
                            and isinstance(origin.table_values, dict)):
                        # A derived local is executable only at values present
                        # in the extractor-proven table relation.  Do not turn
                        # a typed scalar boundary into a fabricated local
                        # value that the table can never produce.
                        table_domain = set()
                        for raw_value in origin.table_values.values():
                            try:
                                table_domain.add(int(raw_value))
                            except (TypeError, ValueError):
                                continue
                        if table_domain:
                            points = tuple(
                                point for point in points
                                if point in table_domain
                            )
                    if control.source == "local":
                        local_domain = {
                            effect.constant_value
                            for effect in ir.local_value_effects
                            if effect.name == control.name
                            and effect.constant_value is not None
                        }
                        if local_domain:
                            points = tuple(
                                point for point in points
                                if point in local_domain
                            )
                points = tuple(
                    point for point in points
                    if not _boundary_conflicts_with_parent(
                        ir, branch, atom, point
                    )
                )
                for point in points:
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
