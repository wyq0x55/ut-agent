"""Deterministic candidate generation from extractor-owned typed facts.

This module is intentionally a generator, not a C parser.  Domains, enum
members, and switch selectors must already be present in FunctionIR.
"""
from __future__ import annotations

from itertools import product
from typing import Mapping

from ut_agent.ir import FunctionIR, TypeInfo, ValueOrigin

PAIRWISE_THRESHOLD = 500


def _domain(type_info: TypeInfo | None):
    if type_info is None:
        return None
    if type_info.enum_values:
        return ("set", set(type_info.enum_values.values()))
    if type_info.kind == "bool":
        return ("set", {0, 1})
    if type_info.min_value is None or type_info.max_value is None:
        return None
    if type_info.min_value == type_info.max_value:
        return ("set", {type_info.min_value})
    return ("range", type_info.min_value, type_info.max_value)


def _in(value, domain) -> bool:
    if domain is None:
        return True
    if domain[0] == "set":
        return value in domain[1]
    return domain[1] <= value <= domain[2]


def _minimum(domain):
    return min(domain[1]) if domain[0] == "set" else domain[1]


def _maximum(domain):
    return max(domain[1]) if domain[0] == "set" else domain[2]


def _median(domain):
    values = (sorted(domain[1]) if domain[0] == "set"
              else [domain[1], domain[2]])
    if domain[0] == "set":
        return values[(len(values) - 1) // 2]
    return domain[1] + (domain[2] - domain[1]) // 2


def _selected_representatives(domain,
                              policy: Mapping[str, object] | None = None) -> set:
    if domain is None:
        return set()
    selected = (("min", "max") if policy is None else tuple(
        policy.get("representative_values", ())))
    representatives = {
        "min": _minimum(domain),
        "median": _median(domain),
        "max": _maximum(domain),
    }
    unknown = sorted({str(name) for name in selected}
                     - set(representatives))
    if unknown:
        raise ValueError(
            "boundary_policy.representative_values 含未知代表值: "
            + ", ".join(unknown)
        )
    return {representatives[name] for name in selected}


def _policy_points(boundary, domain,
                   policy: Mapping[str, object] | None = None) -> set:
    """Return typed representatives selected by the approved policy.

    ``policy=None`` is retained for the legacy, target-neutral helper used by
    standalone enumeration tests.  A resolved baseline always passes its
    formal ``representative_values`` and ``adjacent_constant_values`` fields;
    no alternate policy spelling is interpreted here.
    """
    if boundary is None:
        return set()
    adjacent = (True if policy is None else
                bool(policy.get("adjacent_constant_values", False)))
    points = {boundary} if _in(boundary, domain) else set()
    if adjacent:
        points.update(value for value in (boundary - 1, boundary + 1)
                      if _in(value, domain))
    points.update(_selected_representatives(domain, policy))
    return points


def typed_status_points(boundary, type_info: TypeInfo | None,
                        policy: Mapping[str, object] | None = None) -> tuple:
    """Return status-like representatives for a typed scalar result.

    Stub return values and similar result codes are categorical even when the
    ABI type is an unsigned byte.  Their executable representatives are the
    boundary, its adjacent values, and the typed endpoints; the numeric
    midpoint is not a distinct status category.  This is intentionally an
    explicit extractor/baseline projection, not a type-name heuristic.
    """
    if policy is not None and not bool(policy.get("typed", False)):
        return ()
    domain = _domain(type_info)
    if boundary is None or domain is None:
        return ()
    points = {value for value in (
        boundary, boundary - 1, boundary + 1,
        _minimum(domain), _maximum(domain),
    ) if _in(value, domain)}
    return tuple(sorted(points))


def typed_boundary_points(boundary, type_info: TypeInfo | None,
                          policy: Mapping[str, object] | None = None) -> tuple:
    """Return stable, type-clipped boundary representatives."""
    if policy is not None and not bool(policy.get("typed", False)):
        return ()
    return tuple(sorted(_policy_points(boundary, _domain(type_info), policy)))


def _selector_control(branch, controls):
    selector = branch.selector
    if not isinstance(selector, ValueOrigin):
        return None
    names = {name for name in (selector.driver, selector.expression) if name}
    for control in controls:
        if control.var in names:
            return control
    return None


def _enum_names(type_info: TypeInfo | None) -> dict[int, str]:
    if type_info is None:
        return {}
    return {value: name for name, value in type_info.enum_values.items()}


def _atom_type_info(ir: FunctionIR, atom, control) -> TypeInfo | None:
    """Select an extractor-proven scalar type for a condition operand.

    A dereference atom is serialized with the pointer's ``TypeInfo``.  Its
    finite candidate domain is the extractor-proven pointee domain, whereas a
    NULL guard needs the pointer's two-state null/non-null domain.
    """
    type_info = atom.type_info or control.type_info
    if str(atom.var).replace(" ", "").startswith("*") and type_info is not None:
        if type_info.pointee_info is not None:
            return type_info.pointee_info
    return type_info


def control_candidates(ir: FunctionIR,
                       boundary_policy: Mapping[str, object] | None = None) -> dict:
    """Build candidates from typed atoms and the extractor's selector fact."""
    controls = {control.var: control for control in ir.control_vars}
    candidates: dict = {}

    def add(control, values):
        if not values:
            return
        entry = candidates.setdefault(control.name, {
            "cv": control,
            "values": set(),
            "enum": _enum_names(control.type_info),
        })
        entry["values"].update(values)

    for branch in ir.branches:
        for atom in branch.atoms:
            control = controls.get(atom.var)
            if control is None or control.constant_value is not None:
                continue
            type_info = _atom_type_info(ir, atom, control)
            if atom.boundary is None and atom.boundary_name == "NULL":
                if type_info is not None and type_info.kind == "pointer":
                    add(control, {0, 1})
                continue
            typed_domain = _domain(type_info)
            points = (typed_status_points(atom.boundary, type_info,
                                          boundary_policy)
                      if control.source == "stub"
                      else _policy_points(atom.boundary, typed_domain,
                                          boundary_policy))
            add(control, points)

        if branch.kind != "switch":
            continue
        control = _selector_control(branch, controls.values())
        if control is None or control.constant_value is not None:
            continue
        values = {case.value for case in branch.cases
                  if not case.is_default and case.value is not None}
        if values:
            add(control, values)
            typed_domain = _domain(control.type_info)
            # A default candidate is only valid when it belongs to the
            # extractor-proven selector domain.  Never manufacture a value
            # just past the largest case: that can escape an unsigned or
            # enum contract (for example case 255 -> candidate 256).
            if typed_domain is None:
                add(control, {max(values) + 1})
            else:
                # Search a bounded deterministic set.  Enumerating a proven
                # 32-bit range just to find a default witness is both
                # unnecessary and potentially unbounded in practice.
                lower = int(_minimum(typed_domain))
                upper = int(_maximum(typed_domain))
                probes = [lower, upper, lower + 1, upper - 1]
                probes.extend(
                    lower + offset for offset in range(len(values) + 2)
                )
                default_values = {
                    candidate for candidate in probes
                    if lower <= candidate <= upper
                    and _in(candidate, typed_domain)
                    and candidate not in values
                }
                add(control, default_values)
            if typed_domain is not None:
                if boundary_policy is None or bool(
                        boundary_policy.get("typed", False)):
                    add(control, _selected_representatives(
                        typed_domain, boundary_policy,
                    ))

    return candidates


def settable_columns(ir: FunctionIR, candidates: dict) -> list:
    """Return only externally controllable parameter/global columns."""
    columns = []
    for param in ir.params:
        entry = candidates.get(param.name)
        columns.append((
            param.name,
            entry["cv"] if entry else None,
            sorted(entry["values"]) if entry else [0],
        ))
    for name, entry in candidates.items():
        control = entry["cv"]
        if control.constant_value is not None:
            continue
        if control.source not in {"local_from_global", "global"}:
            continue
        columns.append((name, control, sorted(entry["values"])))
    return columns


def enumerate_rows(ir: FunctionIR, threshold: int = PAIRWISE_THRESHOLD):
    """Enumerate the Cartesian product; pairwise reduction remains explicit TODO."""
    candidates = control_candidates(ir)
    columns = settable_columns(ir, candidates)
    total = 1
    for _, _, values in columns:
        total *= len(values)
    if total > threshold:
        ir.notes.append(
            f"组合数 {total} 超过阈值 {threshold}，应降 pairwise（未实现，仍全量）"
        )
    keys = [name for name, _, _ in columns]
    rows = [
        dict(zip(keys, values))
        for values in product(*(values for _, _, values in columns))
    ]
    return columns, rows
