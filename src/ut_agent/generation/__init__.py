"""Deterministic semantic testcase generation domain."""

from .engine import evaluate_atom, evaluate_branch, generate_intents
from .model import (
    EXECUTION_FAILED, INVALID, NEEDS_REVIEW, UNSUPPORTED, VALIDATED, Constraint,
    EmptyValue, GenerationResult, IntegerValue, PointerValue, RuleTrace,
    SymbolRefValue, TestIntent, TestObligation, TestValue, ValidationResult,
)
from .pack import (
    BUILTIN_PACK, Rule, RulePack, approve_rule_pack, load_rule_pack,
    merge_packs, review_rule_pack, rule_pack_from_mapping,
)
from .obligation import derive_obligations
from .constraint import (
    And, AtomPredicate, BitAndEq, ConditionValue, ConstraintExpr, ConstraintSet,
    Eq, Ge, Gt, InDomain, Le, Lt, Ne, Not, Or, PointerNull, constraints_for,
)
from .solver import SAT, UNSAT, UNKNOWN, SolveResult, solve_obligation
from .evaluator import EvaluationResult, evaluate_obligation
from .oracle import (
    ExternalCall, ExternalEnvironment, OracleResult, build_oracle,
    external_environment,
)
from .suite import TestSuite, generate, generate_suite
from .validation import (
    SuiteValidation, validate_intent_contract, validate_mcdc_pairs, validate_suite,
)

__all__ = [
    "BUILTIN_PACK", "EXECUTION_FAILED", "INVALID", "NEEDS_REVIEW", "UNSUPPORTED", "VALIDATED",
    "Constraint", "EmptyValue", "GenerationResult", "IntegerValue", "PointerValue",
    "Rule", "RulePack", "RuleTrace", "SymbolRefValue", "TestIntent", "TestValue",
    "TestObligation", "ValidationResult", "evaluate_atom", "evaluate_branch",
    "generate_intents", "load_rule_pack", "merge_packs", "review_rule_pack",
    "approve_rule_pack", "rule_pack_from_mapping", "derive_obligations",
    "And", "AtomPredicate", "BitAndEq", "ConditionValue", "ConstraintExpr",
    "ConstraintSet", "Eq", "Ge", "Gt", "InDomain", "Le", "Lt", "Ne", "Not",
    "Or", "PointerNull", "constraints_for", "SAT", "UNSAT", "UNKNOWN",
    "SolveResult", "solve_obligation", "EvaluationResult",
    "evaluate_obligation", "ExternalCall", "ExternalEnvironment",
    "OracleResult", "build_oracle", "external_environment", "TestSuite",
    "generate", "generate_suite", "SuiteValidation", "validate_intent_contract",
    "validate_mcdc_pairs", "validate_suite",
]
