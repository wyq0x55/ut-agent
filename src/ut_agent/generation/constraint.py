"""Typed constraints attached to a single solved obligation.

The old rules engine used a flat ``(variable, operator, value)`` record.  It
is still retained as a compact audit view, but the formal pipeline now also
keeps a small typed constraint AST.  The AST contains extractor-owned symbol
IDs and values only; it never interprets C source text.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ut_agent.ir import FunctionIR

from .model import Constraint, TestObligation


class ConstraintExpr:
    """Marker protocol for serializable typed constraint expressions."""

    kind = "constraint"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[arg-type]


@dataclass(frozen=True)
class AtomPredicate(ConstraintExpr):
    symbol_id: str
    operator: str
    value: Any
    mask: int | None = None
    evidence: str = ""

    kind = "atom_predicate"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self)}


@dataclass(frozen=True)
class Eq(ConstraintExpr):
    symbol_id: str
    value: Any
    evidence: str = ""

    kind = "eq"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self)}


@dataclass(frozen=True)
class Ne(Eq):
    kind = "ne"


@dataclass(frozen=True)
class Lt(Eq):
    kind = "lt"


@dataclass(frozen=True)
class Le(Eq):
    kind = "le"


@dataclass(frozen=True)
class Gt(Eq):
    kind = "gt"


@dataclass(frozen=True)
class Ge(Eq):
    kind = "ge"


@dataclass(frozen=True)
class BitAndEq(ConstraintExpr):
    symbol_id: str
    mask: int
    value: Any
    evidence: str = ""

    kind = "bitand_eq"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self)}


@dataclass(frozen=True)
class InDomain(ConstraintExpr):
    symbol_id: str
    values: tuple[Any, ...]
    evidence: str = ""

    kind = "in_domain"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self), "values": list(self.values)}


@dataclass(frozen=True)
class PointerNull(ConstraintExpr):
    symbol_id: str
    is_null: bool
    evidence: str = ""

    kind = "pointer_null"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self)}


@dataclass(frozen=True)
class ConditionValue(ConstraintExpr):
    symbol_id: str
    value: bool
    pair_id: str
    evidence: str = ""

    kind = "condition_value"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **asdict(self)}


@dataclass(frozen=True)
class And(ConstraintExpr):
    children: tuple[ConstraintExpr, ...]

    kind = "and"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind,
                "children": [child.to_dict() for child in self.children]}


@dataclass(frozen=True)
class Or(ConstraintExpr):
    children: tuple[ConstraintExpr, ...]

    kind = "or"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind,
                "children": [child.to_dict() for child in self.children]}


@dataclass(frozen=True)
class Not(ConstraintExpr):
    child: ConstraintExpr

    kind = "not"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "child": self.child.to_dict()}


@dataclass(frozen=True)
class ConstraintSet:
    obligation_id: str
    constraints: tuple[Constraint, ...] = ()
    expression: ConstraintExpr | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "constraints": [asdict(item) for item in self.constraints],
            "expression": (self.expression.to_dict()
                            if self.expression is not None else None),
        }


def constraints_for(ir: FunctionIR, obligation: TestObligation,
                    assignment: dict[str, Any]) -> ConstraintSet:
    branch = next((item for item in ir.branches
                   if item.bid == obligation.branch_id), None)
    constraints: list[Constraint] = []
    expressions: list[ConstraintExpr] = []
    if branch is not None:
        for atom in branch.atoms:
            constraints.append(Constraint(
                "predicate", atom.var, atom.op, atom.boundary, atom.text,
                symbol_id=atom.var,
            ))
            if atom.mask is not None and atom.boundary is not None:
                expressions.append(BitAndEq(
                    atom.var, int(atom.mask), atom.boundary, atom.text,
                ))
            elif atom.boundary is not None:
                expressions.append(AtomPredicate(
                    atom.var, atom.op, atom.boundary, evidence=atom.text,
                ))
        if obligation.kind in {"mcdc", "condition"} \
                and obligation.condition_index is not None:
            atom = branch.atoms[obligation.condition_index]
            constraints.append(Constraint(
                obligation.kind, atom.var, "condition", obligation.outcome,
                obligation.pair_id or obligation.oid, symbol_id=atom.var,
            ))
            expressions.append(ConditionValue(
                atom.var, bool(obligation.outcome),
                obligation.pair_id or obligation.oid, atom.text,
            ))
        if obligation.kind == "boundary" and obligation.condition_index is not None:
            atom = branch.atoms[obligation.condition_index]
            constraints.append(Constraint(
                "boundary", atom.var, "==", obligation.boundary_value,
                atom.text, symbol_id=atom.var,
            ))
            expressions.append(Eq(atom.var, obligation.boundary_value, atom.text))
    for param in ir.params:
        if param.is_ptr and param.name in assignment:
            constraints.append(Constraint(
                "pointer", param.name, "valid", assignment[param.name],
                "typed pointer domain", symbol_id=param.name,
            ))
            expressions.append(PointerNull(
                param.name, not bool(assignment[param.name]),
                "typed pointer domain",
            ))
    # Witness bindings are explicit typed equalities.  This makes the solver
    # result auditable without treating the witness as a source expression.
    for symbol_id in sorted(assignment):
        if symbol_id.startswith("param:"):
            continue
        expressions.append(Eq(symbol_id, assignment[symbol_id], "solver witness"))
    expression = And(tuple(expressions)) if expressions else None
    return ConstraintSet(obligation.oid, tuple(constraints), expression)


__all__ = [
    "And", "AtomPredicate", "BitAndEq", "ConditionValue", "ConstraintExpr",
    "ConstraintSet", "Eq", "Ge", "Gt", "InDomain", "Le", "Lt", "Ne",
    "Not", "Or", "PointerNull", "constraints_for",
]
