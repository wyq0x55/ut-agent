"""Behavioral gates for the Baseline-driven Issue #2 pipeline."""
from __future__ import annotations

import pytest
from dataclasses import replace

from ut_agent.baseline import load_baseline
from ut_agent.generation import (
    UNSAT, build_oracle, derive_obligations, generate_suite,
    solve_obligation,
)
from ut_agent.ir import (
    Atom, Branch, CallSite, ControlVar, Effect, FunctionIR, Param, TypeInfo,
)
from ut_agent.project import load_project_baselines
from ut_agent.project.model import ProjectManifest, ResolvedProjectContext
from ut_agent.targets.winams.harness import plan_harness
from ut_agent.targets.winams.csv import render_suite_csv


def _context():
    baseline = load_baseline("config/baselines/psd-rebuild-mcdc/1.0.yaml")
    return ResolvedProjectContext(
        ProjectManifest("N-O2606-PSD-049", baseline.id, baseline.version), baseline
    )


def _branch_ir(*, return_effects=None, two_atoms=False):
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=1,
    )
    atoms = [Atom("x", "unsigned char", "==", 1, None, "x == 1", type_info=info)]
    params = [Param("x", "unsigned char", type_info=info)]
    controls = [ControlVar("x", "x", "param", type_info=info, branch_ids=["b0"])]
    connective = None
    tree = {"kind": "atom", "index": 0}
    if two_atoms:
        atoms.append(Atom("y", "unsigned char", "==", 1, None, "y == 1", type_info=info))
        params.append(Param("y", "unsigned char", type_info=info))
        controls.append(ControlVar("y", "y", "param", type_info=info, branch_ids=["b0"]))
        connective = "&&"
        tree = {"kind": "logical", "op": "&&", "children": [
            {"kind": "atom", "index": 0}, {"kind": "atom", "index": 1},
        ]}
    return FunctionIR(
        name="f", file="f.c", line=1, ret_type="void", params=params,
        branches=[Branch(
            bid="b0", kind="if", line=2, atoms=atoms,
            connective=connective, condition_tree=tree,
        )], control_vars=controls, return_effects=return_effects or [],
    )


def test_baseline_pipeline_emits_mcdc_pair_members():
    suite = generate_suite(_branch_ir(two_atoms=True), _context())
    assert suite.status == "VALIDATED"
    pairs = {}
    for intent in suite.intents:
        if intent.obligation.pair_id:
            pairs.setdefault(intent.obligation.pair_id, []).append(intent)
    assert len(pairs) == 2
    assert all({item.obligation.outcome for item in items} == {True, False}
               for items in pairs.values())
    assert all(item.validation.valid for item in suite.intents)
    assert not suite.issues
    assert all(item.constraints.expression is not None
               for item in suite.solves if item.constraints)


def test_project_profile_can_disable_mcdc_without_changing_baseline_version():
    context = _context()
    context = replace(
        context,
        manifest=replace(context.manifest, profile={"mcdc_enabled": False}),
    )
    suite = generate_suite(_branch_ir(two_atoms=True), context)
    assert suite.status == "VALIDATED"
    assert not any(item.obligation.pair_id for item in suite.intents)


def test_unsat_obligation_is_not_projected_to_winams():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=3,
    )
    ir = FunctionIR(
        name="f", file="f.c", line=1, ret_type="void",
        params=[Param("x", "unsigned char", type_info=info)],
        branches=[Branch(
            bid="b0", kind="if", line=2,
            atoms=[Atom("x", "unsigned char", "==", 5, None, "x == 5", type_info=info)],
            condition_tree={"kind": "atom", "index": 0},
        )],
        control_vars=[ControlVar("x", "x", "param", type_info=info, branch_ids=["b0"])],
    )
    obligations = derive_obligations(ir, _context().baseline)
    assert solve_obligation(ir, obligations[0], _context().baseline).status == UNSAT
    suite = generate_suite(ir, _context())
    assert suite.status != "VALIDATED"
    with pytest.raises(ValueError, match="未通过"):
        render_suite_csv(ir, suite)


def test_missing_return_oracle_stays_needs_review():
    ir = _branch_ir(return_effects=[])
    ir.ret_type = "int"
    suite = generate_suite(ir, _context())
    assert suite.status != "VALIDATED"
    assert any("oracle" in issue for issue in suite.issues)


def test_external_environment_is_explicit_and_deterministic():
    ir = _branch_ir()
    ir.calls = [
        CallSite(order=1, callee="dep", line=5, ret_type="int", return_used=True),
        CallSite(order=0, callee="dep", line=4, ret_type="int", return_used=False),
    ]
    suite = generate_suite(ir, _context())
    assert suite.external is not None
    assert suite.external.calls[0].callee == "dep"
    assert suite.external.calls[0].call_orders == (0, 1)
    assert suite.external.calls[0].return_used is True


def test_evaluator_post_state_is_the_only_oracle_source():
    ir = _branch_ir(return_effects=[Effect(path="return", constant_value=7)])
    ir.ret_type = "int"
    suite = generate_suite(ir, _context())
    assert suite.status == "VALIDATED"
    assert suite.evaluations
    assert all(item.complete for item in suite.evaluations)
    assert all(item.post_state["return"] == 7 for item in suite.evaluations)
    assert all(item.expected["return"] == 7 for item in suite.intents)
    assert all("SemanticEvaluator" in value
               for item in suite.intents
               for value in [
                   trace.detail for trace in item.trace if trace.rule_id == "oracle"
               ])


def test_oracle_rejects_raw_assignment_without_evaluator():
    result = build_oracle(_branch_ir(), {"x": 1}, _context().baseline)
    assert result.status == "NEEDS_REVIEW"
    assert "SemanticEvaluator" in result.errors[0]


def test_harness_plan_keeps_pointer_address_out_of_semantics():
    info = TypeInfo(
        canonical_type="unsigned char *", kind="pointer", pointer_depth=1,
        pointee_type="unsigned char",
    )
    ir = FunctionIR(
        name="write", file="write.c", line=1, ret_type="void",
        params=[Param("data", "unsigned char *", is_ptr=True, is_written=True,
                      type_info=info)],
    )
    plan = plan_harness(ir)
    assert plan.memory[0].relation == "non-null"
    assert plan.memory[0].address is None
    assert all("0x1000" not in str(item.to_dict()) for item in plan.memory)


def test_baseline_policy_can_request_condition_and_boundary_obligations():
    context = _context()
    baseline = replace(
        context.baseline,
        condition_policy={"condition_outcome": True},
        boundary_policy={"points": True},
    )
    context = replace(context, baseline=baseline)
    suite = generate_suite(_branch_ir(), context)
    assert suite.status == "VALIDATED"
    assert any(item.obligation.kind == "condition" for item in suite.intents)
    assert any(item.obligation.kind == "boundary" for item in suite.intents)


def test_baseline_loader_rejects_unknown_document_fields(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "baseline:\n"
        "  id: test\n"
        "  version: '1.0'\n"
        "  status: approved\n"
        "  misspelled_policy: true\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_baseline(path)


def test_project_baseline_registry_preserves_version_lock():
    binding = load_project_baselines("config/projects/project-baselines.yaml")[
        "N-O2504-PHD-020"
    ]
    assert binding.baseline_ref == "psd-rebuild-mcdc@1.0"
    assert binding.to_dict()["baseline"] == binding.baseline_ref
