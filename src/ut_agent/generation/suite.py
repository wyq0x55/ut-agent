"""Baseline-driven deterministic generation pipeline and WinAMS gate."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ut_agent.ir import FunctionIR

if TYPE_CHECKING:
    from ut_agent.project.model import ResolvedProjectContext

from .evaluator import EvaluationResult, evaluate_obligation
from .model import (
    NEEDS_REVIEW, RuleTrace, TestIntent, ValidationResult, VALIDATED,
)
from .obligation import derive_obligations
from .oracle import ExternalEnvironment, build_oracle, external_environment
from .solver import SolveResult, SAT, solve_obligation
from .validation import validate_intent_contract, validate_suite


@dataclass(frozen=True)
class TestSuite:
    project_id: str
    baseline_ref: str
    function: str
    status: str
    intents: tuple[TestIntent, ...] = ()
    obligations: tuple[Any, ...] = ()
    solves: tuple[SolveResult, ...] = ()
    evaluations: tuple[EvaluationResult, ...] = ()
    external: ExternalEnvironment | None = None
    issues: tuple[str, ...] = ()
    provenance: dict[str, Any] | None = None

    @property
    def validated_intents(self) -> tuple[TestIntent, ...]:
        if self.status != VALIDATED:
            return ()
        return tuple(item for item in self.intents if item.validation.valid)

    def require_winams(self) -> tuple[TestIntent, ...]:
        if self.status != VALIDATED:
            raise ValueError(
                f"TestSuite 未通过 WinAMS 生成门禁: {self.baseline_ref}; "
                f"{'; '.join(self.issues)}"
            )
        return self.validated_intents

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "baseline_ref": self.baseline_ref,
            "function": self.function,
            "status": self.status,
            "provenance": self.provenance or {},
            "obligations": [item.to_dict() for item in self.obligations],
            "solve_results": [item.to_dict() for item in self.solves],
            "evaluations": [item.to_dict() for item in self.evaluations],
            "external_environment": self.external.to_dict() if self.external else None,
            "issues": list(self.issues),
            "intents": [item.to_dict() for item in self.intents],
        }


def generate_suite(function_ir: FunctionIR,
                   project_context: ResolvedProjectContext) -> TestSuite:
    """Run Baseline → Obligation → Constraint → Solve → Evaluate → Oracle.

    The resolved context is mandatory.  This prevents a caller from silently
    selecting an unversioned policy or using a reference TestCsv as runtime
    input.  All semantic facts still come from the typed FunctionIR.
    """
    baseline = project_context.baseline
    profile = project_context.manifest.profile
    mcdc_enabled = bool(profile.get("mcdc_enabled", False))
    obligations = derive_obligations(
        function_ir, baseline, mcdc_enabled=mcdc_enabled,
        project_rule_pack=project_context.project_rule_pack,
    )
    solves: list[SolveResult] = []
    evaluations: list[EvaluationResult] = []
    intents: list[TestIntent] = []
    issues: list[str] = []
    for obligation in obligations:
        solved = solve_obligation(function_ir, obligation, baseline)
        solves.append(solved)
        if solved.status != SAT or solved.assignment is None:
            issues.append(f"{obligation.oid}: {solved.status} {solved.reason}".strip())
            intents.append(TestIntent(
                case_id=f"U{len(intents) + 1:03d}", obligation=obligation,
                trace=(RuleTrace("baseline", baseline.ref, "obligation unresolved"),),
                validation=ValidationResult(
                    NEEDS_REVIEW, errors=(f"solver {solved.status}: {solved.reason}",)
                ),
            ))
            continue
        assignment = dict(solved.assignment)
        evaluation = evaluate_obligation(function_ir, obligation, assignment)
        evaluations.append(evaluation)
        oracle = build_oracle(function_ir, evaluation, baseline)
        expected = dict(oracle.values)
        intent = TestIntent(
            case_id=f"U{len(intents) + 1:03d}", obligation=obligation,
            inputs=assignment, expected=expected,
            constraints=(solved.constraints.constraints
                         if solved.constraints else ()),
            trace=(
                RuleTrace("baseline", baseline.ref, "approved TestBaseline"),
                RuleTrace("solver", f"{solved.status}:{solved.checked}",
                          "finite typed-domain proof"),
                RuleTrace("oracle", ",".join(sorted(oracle.evidence)),
                           "SemanticEvaluator post-state oracle"),
            ),
        )
        validation = validate_intent_contract(
            function_ir, intent, baseline,
            evaluation=evaluation, oracle=oracle,
        )
        if not validation.valid:
            issues.extend(validation.errors)
        intents.append(TestIntent(**{**intent.__dict__, "validation": validation}))
    suite_validation = validate_suite(
        intents, unresolved=issues, baseline=baseline, ir=function_ir,
    )
    return TestSuite(
        project_id=project_context.project_id,
        baseline_ref=baseline.ref,
        function=function_ir.name,
        status=suite_validation.status,
        intents=tuple(intents), obligations=obligations, solves=tuple(solves),
        evaluations=tuple(evaluations),
        external=external_environment(function_ir),
        issues=suite_validation.errors,
        provenance={**project_context.provenance,
                    "function": function_ir.name,
                    "source": function_ir.file},
    )


def generate(function_ir: FunctionIR,
             resolved_project_context: ResolvedProjectContext) -> TestSuite:
    """Formal generation entry point: FunctionIR plus resolved context."""
    return generate_suite(function_ir, resolved_project_context)
