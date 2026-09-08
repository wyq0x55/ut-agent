"""Issue #6 project-corpus manifest and semantic comparison gates."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

from ut_agent.baseline import load_baseline
from ut_agent.learning.compare import compare_testcsv
from ut_agent.learning import label_kind, normalize_golden_csv, normalize_label
from ut_agent.generation.boundary import control_candidates, typed_boundary_points
from ut_agent.generation.obligation import derive_obligations
from ut_agent.generation.solver import solve_obligation
from ut_agent.generation.engine import (
    _control_env, _local_value, _pointer_output_values,
)
from ut_agent.generation.model import TestObligation as GenerationObligation
from ut_agent.ir import (
    Atom, Branch, CallSite, ControlVar, Effect, FunctionIR, GlobalObject,
    Param, TypeInfo,
    ValueOrigin,
)
from ut_agent.project import load_manifest
from ut_agent.reporting import (
    AMBIGUOUS_MATCH,
    CALIBRATION_CLASSIFICATIONS,
    STANDARD_GAP_CATEGORIES,
    EQUIVALENT_REPRESENTATIVE,
    EXTRA_GENERATED,
    PARTIAL_MATCH,
    build_corpus_validation_report,
    compare_function_semantics,
    golden_for_unit,
    match_semantic_cases,
    load_corpus_manifest,
    preflight_corpus,
    validate_corpus_paths,
)
from ut_agent.targets.winams.csv import _intent_value, _pointer_column_key
from ut_agent.targets.winams.index import load_index


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "config" / "projects" / "N-O2608-PSD-087.corpus.json"


def _golden() -> Path:
    return next((ROOT / "examples" / "N-O2608-PSD-087" / "winAMS").rglob(
        "p_u1l_mem_req_read_ramdf1.csv"
    ))


def test_issue6_corpus_manifest_locks_all_indexed_functions():
    manifest = load_corpus_manifest(CORPUS)
    validate_corpus_paths(manifest)
    context = load_manifest(manifest.context_manifest)
    assert manifest.project_id == "N-O2608-PSD-087"
    assert manifest.scope == "all-indexed-functions"
    assert context.baseline_ref == "psd-rebuild@1.1"
    assert "baseline" not in manifest.to_dict()["project"]


def test_issue6_missing_indexed_source_is_reported_as_blocked_fixture():
    manifest = load_corpus_manifest(CORPUS)
    blocked = preflight_corpus(
        replace(manifest, product_root=ROOT / ".tmp" / "missing-issue6-product")
    )
    assert len(blocked) == 6
    assert {item["status"] for item in blocked} == {"BLOCKED"}
    assert {item["reason"] for item in blocked} == {"FIXTURE_MISSING"}


def test_issue12_project_report_exposes_calibration_contract(tmp_path: Path):
    manifest = load_corpus_manifest(CORPUS)
    report = build_corpus_validation_report(
        manifest, load_manifest(manifest.context_manifest), (),
        output_root=tmp_path,
    )
    assert report["calibration"]["golden_role"] == "detector_only"
    assert report["calibration"]["source_evidence"] == {
        "baseline_document": str(manifest.baseline_document),
        "baseline_sheet": manifest.baseline_sheet,
        "baseline_revision": manifest.baseline_revision,
    }
    assert report["totals"]["indexed_functions"] == 6
    assert report["totals"]["missing_functions"] == 6
    assert report["totals"]["root_gap_count"] == 6
    assert report["totals"]["gap_categories"] == {"RUN_INCOMPLETE": 6}
    assert report["taxonomy"]["calibration_classifications"] == list(
        CALIBRATION_CLASSIFICATIONS
    )


def test_issue12_present_golden_is_distinct_from_not_inspected(tmp_path: Path):
    manifest = load_corpus_manifest(CORPUS)
    context = load_manifest(manifest.context_manifest)
    row = next(
        item for item in load_index(manifest.index_csv, manifest.product_root)
        if item.function == "p_u1l_mem_req_read_ramdf"
    )
    output_dir = tmp_path / row.target_rel
    output_dir.mkdir(parents=True)
    summary = {
        "schema_version": 1, "status": "VALIDATED", "intent_count": 1000,
        "csv_intent_count": 1000, "validated_intent_count": 1000,
        "obligation_kinds": {"branch": 1000}, "solve_statuses": {"SAT": 1000},
        "evaluation_count": 1000, "evaluation_complete_count": 1000,
        "issues": [], "input_keys": [], "expected_keys": [], "stub_keys": [],
    }
    (output_dir / "test-intents-summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    unit = SimpleNamespace(
        row=row,
        status="VALIDATED",
        testcsv=output_dir / "TestCsv" / f"{row.function}.csv",
        intent_manifest=output_dir / "test-intents.json",
    )
    report = build_corpus_validation_report(
        manifest, context, (unit,), output_root=tmp_path,
    )
    item = next(item for item in report["functions"] if item["function"] == row.function)
    assert item["golden"]["file_status"] == "PRESENT"
    assert item["golden"]["inspection_status"] == "NOT_INSPECTED"
    assert item["golden"]["status"] == "PRESENT_NOT_INSPECTED"
    assert report["totals"]["golden_not_inspected"] >= 1


def test_issue12_missing_generation_summary_is_unknown_not_zero(tmp_path: Path):
    manifest = load_corpus_manifest(CORPUS)
    context = load_manifest(manifest.context_manifest)
    row = load_index(manifest.index_csv, manifest.product_root)[0]
    unit = SimpleNamespace(
        row=row,
        status="NEEDS_REVIEW",
        testcsv=tmp_path / "missing.csv",
        intent_manifest=tmp_path / "missing-test-intents.json",
    )
    report = build_corpus_validation_report(
        manifest, context, (unit,), output_root=tmp_path,
    )
    assert report["totals"]["generated_intents"] is None
    assert report["totals"]["generated_intents_unknown_functions"] == 6
    processed = next(item for item in report["functions"] if item["row"] == row.row_number)
    assert processed["generated"]["semantics"]["intent_count"] is None


def test_issue12_golden_mapping_keeps_src_below_n_o2606_root():
    manifest = load_corpus_manifest(
        ROOT / "config" / "projects" / "N-O2606-PSD-049.corpus.json"
    )
    row = load_index(manifest.index_csv, manifest.product_root)[0]
    golden = golden_for_unit(manifest, SimpleNamespace(row=row))
    assert golden is not None
    assert golden.is_file()
    assert golden.parts[-4:] == (
        "p_blm.c", "p_vol_blm_job_out", "TestCsv", "p_vol_blm_job_out.csv"
    )


def test_issue12_boundary_solver_uses_single_typed_representative():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    names = [f"g{index}" for index in range(8)]
    ir = FunctionIR(
        name="synthetic_boundary_product", file="target.c", line=1,
        ret_type="void",
        branches=[Branch(
            bid="b0", kind="if", line=2, connective="&&",
            atoms=[Atom(name, "unsigned char", "==", 1, None,
                        f"{name} == 1", type_info=info) for name in names],
        )],
        control_vars=[
            ControlVar(name, name, "global", type_info=info) for name in names
        ],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml")
    result = solve_obligation(
        ir,
        GenerationObligation(
            oid="b0:boundary:0:below:0", kind="boundary", branch_id="b0",
            condition_index=0, boundary_value=0, boundary_class="below",
        ),
        baseline,
    )
    assert result.status == "SAT"
    assert result.checked == 1


def test_issue12_boundary_solver_uses_representative_under_product_guard():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    names = ["g0", "g1"]
    ir = FunctionIR(
        name="synthetic_small_boundary_product", file="target.c", line=1,
        ret_type="void",
        branches=[Branch(
            bid="b0", kind="if", line=2, connective="&&",
            atoms=[Atom(name, "unsigned char", "==", 1, None,
                        f"{name} == 1", type_info=info) for name in names],
        )],
        control_vars=[
            ControlVar(name, name, "global", type_info=info) for name in names
        ],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml")
    result = solve_obligation(
        ir,
        GenerationObligation(
            oid="b0:boundary:0:exact:1", kind="boundary", branch_id="b0",
            condition_index=0, boundary_value=1, boundary_class="exact",
        ),
        baseline,
    )
    assert result.status == "SAT"
    assert result.checked == 1


def test_issue12_targeted_solver_honors_nested_parent_outcome():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="synthetic_nested_parent_path", file="target.c", line=1,
        ret_type="void",
        branches=[
            Branch(
                bid="parent", kind="if", line=2,
                atoms=[Atom("mode", "unsigned char", "==", 2, None,
                             "mode == 2", type_info=info)],
            ),
            Branch(
                bid="child", kind="if", line=3, parent_bid="parent",
                parent_outcome=True,
                atoms=[Atom("polarity", "unsigned char", "==", 1, None,
                             "polarity == 1", type_info=info)],
            ),
        ],
        control_vars=[
            ControlVar("mode", "mode", "global", type_info=info),
            ControlVar("polarity", "polarity", "global", type_info=info),
        ],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml")
    result = solve_obligation(
        ir,
        GenerationObligation(
            oid="child:T", kind="branch", branch_id="child", outcome=True,
        ),
        baseline,
    )
    assert result.status == "SAT"
    assert result.assignment["mode"] == 2


def test_issue12_mcdc_uses_branch_slice_over_large_global_product():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    names = ["left", "right"] + [f"unrelated{index}" for index in range(6)]
    branches = [Branch(
        bid="mcdc", kind="if", line=2, connective="&&",
        atoms=[
            Atom("left", "unsigned char", "==", 1, None,
                 "left == 1", type_info=info),
            Atom("right", "unsigned char", "==", 1, None,
                 "right == 1", type_info=info),
        ],
    )]
    branches.extend(
        Branch(
            bid=f"noise{index}", kind="if", line=3 + index,
            atoms=[Atom(name, "unsigned char", "==", 1, None,
                        f"{name} == 1", type_info=info)],
        )
        for index, name in enumerate(names[2:])
    )
    ir = FunctionIR(
        name="synthetic_mcdc_slice", file="target.c", line=1,
        ret_type="void", branches=branches,
        control_vars=[ControlVar(name, name, "global", type_info=info)
                      for name in names],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml")
    result = solve_obligation(
        ir,
        GenerationObligation(
            oid="mcdc:mcdc:0:T", kind="mcdc", branch_id="mcdc",
            condition_index=0, outcome=True,
        ),
        baseline,
    )
    assert result.status == "SAT"
    assert result.assignment["left"] == 1
    assert result.assignment["right"] == 1


def test_issue12_mcdc_honors_nested_condition_tree():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    branch = Branch(
        bid="nested", kind="if", line=2, connective="&&",
        atoms=[
            Atom("can", "unsigned char", "==", 1, None,
                 "can == 1", type_info=info),
            Atom("open", "unsigned char", "==", 1, None,
                 "open == 1", type_info=info),
            Atom("close", "unsigned char", "==", 2, None,
                 "close == 2", type_info=info),
        ],
        condition_tree={
            "kind": "logical", "op": "&&", "children": [
                {"kind": "atom", "index": 0},
                {"kind": "logical", "op": "||", "children": [
                    {"kind": "atom", "index": 1},
                    {"kind": "atom", "index": 2},
                ]},
            ],
        },
    )
    ir = FunctionIR(
        name="synthetic_nested_mcdc", file="target.c", line=1,
        ret_type="void", branches=[branch],
        control_vars=[ControlVar(name, name, "global", type_info=info)
                      for name in ("can", "open", "close")],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" /
                             "psd-rebuild" / "1.1.yaml")
    obligations = [
        item for item in derive_obligations(ir, baseline, mcdc_enabled=True)
        if item.kind == "mcdc"
    ]
    assert len(obligations) == 6
    results = [solve_obligation(ir, item, baseline) for item in obligations]
    assert all(item.status == "SAT" for item in results)
    middle_true = next(item for item in results
                       if item.obligation.condition_index == 1
                       and item.obligation.outcome is True)
    assert middle_true.assignment["can"] == 1
    assert middle_true.assignment["open"] == 1
    assert middle_true.assignment["close"] != 2


def test_issue12_mcdc_keeps_duplicate_leaf_paths_as_separate_inputs():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="synthetic_indexed_field_mcdc", file="target.c", line=1,
        ret_type="void", branches=[Branch(
            bid="fields", kind="if", line=2, connective="&&",
            atoms=[
                Atom("table[0].field", "unsigned char", "==", 0, None,
                     "table[0].field == 0", type_info=info),
                Atom("table[1].field", "unsigned char", "==", 0, None,
                     "table[1].field == 0", type_info=info),
            ],
        )],
        control_vars=[
            ControlVar("field", "table[0].field", "global", type_info=info),
            ControlVar("field", "table[1].field", "global", type_info=info),
        ],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" /
                             "psd-rebuild" / "1.1.yaml")
    obligation = GenerationObligation(
        oid="fields:mcdc:0:F", kind="mcdc", branch_id="fields",
        condition_index=0, outcome=False,
    )
    result = solve_obligation(ir, obligation, baseline)
    assert result.status == "SAT"
    assert result.assignment["table[0].field"] != result.assignment["table[1].field"]


def test_issue12_boundary_uses_alternate_nested_path_witness():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="synthetic_nested_boundary_path", file="target.c", line=1,
        ret_type="void",
        branches=[
            Branch(
                bid="b0", kind="if", line=2,
                atoms=[Atom("rr", "unsigned char", "==", 2, None,
                             "rr == 2", type_info=info)],
            ),
            Branch(
                bid="b1", kind="if", line=3, parent_bid="b0",
                parent_outcome=False, atoms=[
                    Atom("otsw", "unsigned char", "==", 1, None,
                         "otsw == 1", type_info=info),
                    Atom("rr", "unsigned char", "==", 1, None,
                         "rr == 1", type_info=info),
                ], connective="&&",
            ),
            Branch(
                bid="b2", kind="elseif", line=4, parent_bid="b1",
                parent_outcome=False, chain_index=1, atoms=[
                    Atom("otsw", "unsigned char", "==", 0, None,
                         "otsw == 0", type_info=info),
                    Atom("rr", "unsigned char", "==", 1, None,
                         "rr == 1", type_info=info),
                ], connective="&&",
            ),
            Branch(
                bid="b3", kind="elseif", line=5, parent_bid="b2",
                parent_outcome=False, chain_index=2, atoms=[
                    Atom("otsw", "unsigned char", "==", 0, None,
                         "otsw == 0", type_info=info),
                    Atom("rr", "unsigned char", "==", 0, None,
                         "rr == 0", type_info=info),
                ], connective="&&",
            ),
        ],
        control_vars=[
            ControlVar("rr", "rr", "global", type_info=info),
            ControlVar("otsw", "otsw", "global", type_info=info),
        ],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml")
    result = solve_obligation(
        ir,
        GenerationObligation(
            oid="b3:boundary:1:above:1", kind="boundary", branch_id="b3",
            condition_index=1, boundary_value=1, boundary_class="above",
        ),
        baseline,
    )
    assert result.status == "SAT"
    assert result.assignment["rr"] == 1
    assert result.assignment["otsw"] not in {0, 1}


def test_issue12_local_boundary_uses_pre_assignment_guard():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="synthetic_local_assignment_path", file="target.c", line=1,
        ret_type="void",
        branches=[
            Branch(
                bid="b0", kind="if", line=2,
                atoms=[Atom("gate", "unsigned char", "==", 1, None,
                             "gate == 1", type_info=info)],
            ),
            Branch(
                bid="b1", kind="if", line=3,
                atoms=[Atom("local_flag", "unsigned char", "==", 0, None,
                             "local_flag == 0", type_info=info)],
            ),
        ],
        control_vars=[
            ControlVar("gate", "gate", "global", type_info=info),
            ControlVar(
                "local_flag", "local_flag", "local", type_info=info,
                value_origin=ValueOrigin(kind="constant"),
            ),
        ],
        local_value_effects=[
            Effect(name="local_flag", constant_value=1, source_offset=10),
            Effect(
                name="local_flag", constant_value=0, source_offset=20,
                guards=[{"bid": "b0", "then": True}],
            ),
        ],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" /
                             "psd-rebuild" / "1.1.yaml")
    result = solve_obligation(
        ir,
        GenerationObligation(
            oid="b1:boundary:0:above:1", kind="boundary", branch_id="b1",
            condition_index=0, boundary_value=1, boundary_class="above",
        ),
        baseline,
    )
    assert result.status == "SAT"
    assert result.assignment["gate"] != 1


def test_issue12_stub_param_field_binds_local_control_to_call_slot():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="synthetic_structured_stub_output", file="target.c", line=1,
        ret_type="void",
        calls=[CallSite(order=0, callee="pal_get_record", line=2)],
        branches=[Branch(
            bid="b0", kind="if", line=3,
            atoms=[Atom("status", "unsigned char", "==", 1, None,
                        "status == 1", type_info=info)],
        )],
        control_vars=[ControlVar(
            "status", "status", "local", type_info=info,
            value_origin=ValueOrigin(
                kind="stub_param", callee="pal_get_record", index="0",
                call_order=0, field="status",
            ),
        )],
        local_value_effects=[Effect(
            name="status", value="record.status",
            origin=ValueOrigin(
                kind="stub_param", callee="pal_get_record", index="0",
                call_order=0, field="status",
            ),
        )],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" /
                             "psd-rebuild" / "1.1.yaml")
    result = solve_obligation(
        ir,
        GenerationObligation(
            oid="b0:T", kind="branch", branch_id="b0", outcome=True,
        ),
        baseline,
    )
    assert result.status == "SAT"
    assert result.assignment[
        "call:pal_get_record:param:0:0.status"
    ] == 1


def test_issue12_pointer_output_evaluates_extractor_expression_tree():
    ir = FunctionIR(
        name="synthetic_pointer_expression", file="target.c", line=1,
        ret_type="void",
        globals_used=["source"],
        params=[Param(
            name="out", type="u1 *", is_ptr=True, is_written=True,
            write_effects=[Effect(
                path="out[0]", value="value",
                origin=ValueOrigin(
                    kind="local",
                    expression_tree={
                        "kind": "reference", "name": "value",
                    },
                ),
            )],
        )],
        local_value_effects=[
            Effect(
                name="value", value="(source & 256) >> 8",
                source_offset=1,
                origin=ValueOrigin(
                    kind="local",
                    expression_tree={
                        "kind": "binary", "op": ">>",
                        "lhs": {
                            "kind": "binary", "op": "&",
                            "lhs": {"kind": "reference", "name": "source"},
                            "rhs": {"kind": "constant", "value": 256},
                        },
                        "rhs": {"kind": "constant", "value": 8},
                    },
                ),
            ),
            Effect(
                name="value", value="(source & 512) >> 8", operator="|=",
                source_offset=2,
                origin=ValueOrigin(
                    kind="local",
                    expression_tree={
                        "kind": "binary", "op": ">>",
                        "lhs": {
                            "kind": "binary", "op": "&",
                            "lhs": {"kind": "reference", "name": "source"},
                            "rhs": {"kind": "constant", "value": 512},
                        },
                        "rhs": {"kind": "constant", "value": 8},
                    },
                ),
            ),
        ],
    )

    selected = {"global:source": 0x300, "source": 0x300}
    env = _control_env(selected, ir)
    assert env["source"] == 0x300
    assert _local_value(ir, "value", env) == 3
    values = _pointer_output_values(ir, ir.params[0], selected)

    assert values == {"param:out:pointee:out[0]": 3}


def test_issue12_boundary_skips_const_table_parent_conflict():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="synthetic_table_parent_path", file="target.c", line=1,
        ret_type="void",
        branches=[
            Branch(
                bid="parent", kind="if", line=2,
                atoms=[Atom("derived", "unsigned char", "!=", 255, None,
                             "derived != 255", type_info=info)],
            ),
            Branch(
                bid="child", kind="if", line=3, parent_bid="parent",
                parent_outcome=False,
                atoms=[Atom("index", "unsigned char", "==", 56, None,
                             "index == 56", type_info=info)],
            ),
        ],
        control_vars=[
            ControlVar("index", "index", "param", type_info=info),
            ControlVar(
                "derived", "derived", "derived", type_info=info,
                value_origin=ValueOrigin(
                    kind="const_table_field", driver="index",
                    table_values={"0": 0, "56": 255},
                ),
            ),
        ],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml")
    obligations = derive_obligations(ir, baseline)
    child_points = {
        item.boundary_value for item in obligations
        if item.branch_id == "child" and item.kind == "boundary"
    }
    assert 0 not in child_points


def test_issue6_baseline_keeps_source_mapped_approved_rules():
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml")
    assert baseline.ref == "psd-rebuild@1.1"
    assert len(baseline.rules) == 8
    assert {item["status"] for item in baseline.rules} == {"approved"}
    assert {item["rule_id"] for item in baseline.rules} >= {
        "psd.0-2.typed-domain", "psd.4.mcdc", "psd.6.order",
    }
    assert baseline.array_policy["comparison_classes"]["table_array"] == {
        "index_coverage": "all"
    }


def test_issue6_boundary_policy_uses_formal_representative_fields():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=10,
    )
    assert typed_boundary_points(
        4, info,
        {"typed": True, "representative_values": ["min"],
         "adjacent_constant_values": False},
    ) == (0, 4)
    assert typed_boundary_points(
        4, info,
        {"typed": True, "representative_values": ["median", "max"],
         "adjacent_constant_values": True},
    ) == (3, 4, 5, 10)


def test_issue12_typed_boundary_clips_u8_endpoint_adjacency():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    policy = {
        "typed": True, "representative_values": ["min", "median", "max"],
        "adjacent_constant_values": True,
    }
    assert typed_boundary_points(255, info, policy) == (0, 127, 254, 255)
    assert typed_boundary_points(0, info, policy) == (0, 1, 127, 255)


def test_issue12_obligations_use_stub_status_and_proven_index_domains():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="synthetic_index_and_stub_domains", file="target.c", line=1,
        ret_type="void",
        branches=[Branch(
            bid="b0", kind="if", line=2, connective="&&",
            atoms=[
                Atom("index", "unsigned char", "==", 3, None,
                     "index == 3", type_info=info),
                Atom("stub_status", "unsigned char", "==", 1, None,
                     "stub_status == 1", type_info=info),
            ],
        )],
        control_vars=[
            ControlVar("index", "index", "global", type_info=info),
            ControlVar("stub_status", "stub_status", "stub", type_info=info),
        ],
        global_objects=[GlobalObject(
            name="table", read=True, array_sizes=[3],
            index_drivers=["index"],
        )],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml")
    obligations = derive_obligations(ir, baseline)
    index_points = {
        item.boundary_value for item in obligations
        if item.kind == "boundary" and item.condition_index == 0
    }
    stub_points = {
        item.boundary_value for item in obligations
        if item.kind == "boundary" and item.condition_index == 1
    }
    assert index_points == {0, 2}
    assert stub_points == {0, 1, 2, 255}
    assert 127 not in stub_points


def test_issue6_case_matching_separates_free_values_from_required_values():
    generated = [{
        "case_id": "U001", "kind": "branch_outcome", "label": "TRUE",
        "outcome": True, "truth_vector": None, "identity": {},
        "inputs": {"x": 7}, "expected": {"return": 0},
        "required_input_values": {}, "required_expected_values": {},
        "stub": {"columns": [], "values": {}},
        "oracle": {"columns": ["return"], "values": {"return": 0}},
    }]
    golden = [{
        "case_id": "U017", "kind": "branch_outcome", "label": "TRUE",
        "outcome": True, "truth_vector": None, "identity": {},
        "inputs": {"x": 9}, "expected": {"return": 0},
        "required_input_values": {}, "required_expected_values": {},
        "stub": {"columns": [], "values": {}},
        "oracle": {"columns": ["return"], "values": {"return": 0}},
    }]
    equivalent = match_semantic_cases(generated, golden)
    assert equivalent["counts"] == {EQUIVALENT_REPRESENTATIVE: 1}
    golden[0]["required_input_values"] = {"x": 1}
    partial = match_semantic_cases(generated, golden)
    assert partial["counts"] == {PARTIAL_MATCH: 1}


def test_issue12_gate_failure_skips_unadjudicable_case_differences():
    def case(case_id: str, value: int) -> dict:
        return {
            "case_id": case_id, "kind": "branch_outcome", "label": "TRUE",
            "outcome": True, "truth_vector": None, "identity": {},
            "inputs": {"x": value}, "expected": {"return": 0},
            "required_input_values": {"x": value},
            "required_expected_values": {},
            "stub": {"columns": [], "values": {}},
            "oracle": {"columns": ["return"], "values": {"return": 0}},
        }

    golden = {
        "testcase_count": 1, "viewpoints": {}, "condition_combinations": [],
        "boundary_domain": {}, "stub": {}, "oracle": {},
        "required_values": {}, "projection": {},
    }
    result = compare_function_semantics(
        function="synthetic_u8", generated_manifest={
            "status": "NEEDS_REVIEW", "intent_count": 1,
            "issues": ["solver UNSAT for out-of-domain value"],
            "solve_statuses": {"UNSAT": 1}, "expected_keys": [],
            "input_keys": [], "obligation_kinds": {}, "boundary_classes": {},
            "stub_keys": [],
        },
        generated_csv=None, golden=golden,
        actual_csv_path=ROOT / ".tmp" / "synthetic.csv",
        golden_csv_path=ROOT / ".tmp" / "golden.csv",
        generated_cases=[case("generated", 254)],
        golden_cases=[case("golden", 255)],
    )
    (root,) = result["gaps"]
    assert root["category"] == "SOLVER_GAP"
    assert root["root_cause_status"] == "CANDIDATE_ROOT"
    assert result["case_matching"]["status"] == "SKIPPED_GENERATION_GATE"
    assert root["calibration"]["classification"] == "NEEDS_REVIEW"
    assert "IMPLEMENTATION_DRIFT" in CALIBRATION_CLASSIFICATIONS


def test_issue12_match_budget_preserves_needs_review_without_case_replay():
    golden = normalize_golden_csv(_golden())
    result = compare_function_semantics(
        function="budgeted_function",
        generated_manifest={
            "status": "VALIDATED", "intent_count": 195,
            "issues": [], "solve_statuses": {}, "expected_keys": [],
            "input_keys": [], "obligation_kinds": {}, "boundary_classes": {},
            "stub_keys": [],
        },
        generated_csv=None,
        golden=golden,
        actual_csv_path=ROOT / ".tmp" / "generated.csv",
        golden_csv_path=_golden(),
        matching_budget={
            "status": "MATCHING_BUDGET_EXCEEDED", "pair_budget": 4096,
            "candidate_pairs": 20_865,
        },
    )
    assert result["equivalence"] == "MATCHING_BUDGET_EXCEEDED"
    assert result["case_matching"]["candidate_pairs"] == 20_865
    assert result["gaps"][0]["dimension"] == "semantic_match_budget"


def test_issue6_case_matching_reports_golden_row_order():
    def case(case_id: str, value: int) -> dict:
        return {
            "case_id": case_id, "kind": "branch_outcome", "label": "TRUE",
            "outcome": True, "truth_vector": None, "identity": {},
            "inputs": {"x": value}, "expected": {"return": value},
            "required_input_values": {"x": value},
            "required_expected_values": {"return": value},
            "stub": {"columns": [], "values": {}},
            "oracle": {"columns": ["return"], "values": {"return": value}},
        }

    result = match_semantic_cases(
        [case("generated-1", 1), case("generated-2", 2)],
        [case("golden-2", 2), case("golden-1", 1)],
    )
    assert result["row_count_equal"] is True
    assert result["row_order_equal"] is False
    assert result["row_order"]["mismatches"]


def test_issue12_case_matching_keeps_cross_viewpoint_ambiguity():
    def case(case_id: str, kind: str) -> dict:
        return {
            "case_id": case_id, "kind": kind, "label": "",
            "outcome": None, "truth_vector": None, "identity": {},
            "inputs": {}, "expected": {},
            "required_input_values": {}, "required_expected_values": {},
            "stub": {"columns": [], "values": {}},
            "oracle": {"columns": [], "values": {}},
        }

    result = match_semantic_cases(
        [case("generated-execution", "execution"), case("generated-loop", "loop")],
        [case("golden", "unlabelled")],
    )
    assert result["counts"] == {AMBIGUOUS_MATCH: 1, EXTRA_GENERATED: 2}


def test_issue6_case_matching_reports_structural_row_order_separately():
    def case(case_id: str, label: str) -> dict:
        return {
            "case_id": case_id, "kind": "branch", "label": label,
            "outcome": True, "truth_vector": None, "identity": {},
            "inputs": {"x": label}, "expected": {"return": 0},
            "required_input_values": {}, "required_expected_values": {},
            "stub": {"columns": [], "values": {}},
            "oracle": {"columns": ["return"], "values": {"return": 0}},
        }

    result = match_semantic_cases(
        [case("generated-1", "if (x == 0)"), case("generated-2", "if (x == 1)")],
        [case("golden-2", "if (x == 1)"), case("golden-1", "if (x == 0)")],
    )
    assert result["row_order_equal"] is False
    assert result["structural_row_order_equal"] is False
    assert result["structural_row_order"]["mismatches"]


def test_issue6_free_representative_values_are_not_exact_value_matches(tmp_path: Path):
    golden_path = _golden()
    actual_path = tmp_path / "generated.csv"
    data = golden_path.read_bytes().replace(b"0x5400", b"0x5600", 1)
    actual_path.write_bytes(data)
    golden = normalize_golden_csv(golden_path)
    actual = normalize_golden_csv(actual_path)
    result = compare_function_semantics(
        function="p_u1l_mem_req_read_ramdf",
        generated_manifest={
            "status": "VALIDATED", "intent_count": actual["testcase_count"],
            "issues": [], "solve_statuses": {}, "expected_keys": [],
            "input_keys": [], "obligation_kinds": {}, "boundary_classes": {},
            "stub_keys": [],
        },
        generated_csv=actual,
        golden=golden,
        actual_csv_path=actual_path,
        golden_csv_path=golden_path,
    )
    assert result["equivalence"] == "FREE_REPRESENTATIVE_EQUIVALENT"
    assert result["gaps"] == []


def test_issue6_normalizes_mcdc_labels_to_truth_vectors():
    assert normalize_label("組合せ(F || T => F(2))") == "F||T=>F"
    assert label_kind("組合せ(F || T => F(2))") == "condition_combination"
    assert label_kind("F || T => F") == "condition_combination"


def test_issue6_mismatch_is_classified_as_baseline_gap(tmp_path: Path):
    golden_path = _golden()
    actual_path = tmp_path / "generated.csv"
    actual_path.write_bytes(golden_path.read_bytes())
    golden = normalize_golden_csv(golden_path)
    generated = dict(golden)
    generated["testcase_count"] = golden["testcase_count"] - 1
    result = compare_function_semantics(
        function="f",
        generated_manifest={
            "status": "VALIDATED", "intent_count": generated["testcase_count"],
            "issues": [], "solve_statuses": {}, "expected_keys": [],
            "input_keys": [], "obligation_kinds": {}, "boundary_classes": {},
            "stub_keys": [],
        },
        generated_csv=generated,
        golden=golden,
        actual_csv_path=actual_path,
        golden_csv_path=golden_path,
    )
    assert result["equivalence"] == "SEMANTIC_DIFFERENCE"
    assert result["gaps"]
    assert all(item["category"] in STANDARD_GAP_CATEGORIES for item in result["gaps"])
    assert all(item["review_required"] for item in result["gaps"])


def test_issue6_compare_uses_explicit_expected_file(tmp_path: Path):
    golden_path = _golden()
    actual = tmp_path / "generated.csv"
    actual.write_bytes(golden_path.read_bytes())
    project = SimpleNamespace(units=(SimpleNamespace(
        name="f", testcsv=actual, expected=golden_path,
    ),))
    assert compare_testcsv(project) == [("f", True)]


def test_issue6_pointer_guards_and_dereferences_use_typed_domains():
    pointee = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    pointer = TypeInfo(
        canonical_type="unsigned char *", kind="pointer", pointer_depth=1,
        pointee_type="unsigned char", pointee_info=pointee,
    )
    ir = FunctionIR(
        name="pointer_target", file="target.c", line=1, ret_type="void",
        params=[Param("ptr", "unsigned char *", is_ptr=True, type_info=pointer)],
        branches=[
            Branch(
                bid="null", kind="if", line=2,
                atoms=[Atom("ptr", "unsigned char *", "!=", None,
                            "NULL", "ptr != NULL",
                            type_info=pointer)],
            ),
            Branch(
                bid="value", kind="if", line=3,
                    parent_bid="null",
                    atoms=[Atom("*ptr", "unsigned char *", "==", 4,
                                None, "*ptr == 4", type_info=pointer)],
            ),
        ],
        control_vars=[
            ControlVar("ptr", "ptr", "param", type_info=pointer),
            ControlVar("*ptr", "*ptr", "param", type_info=pointer),
        ],
    )
    candidates = control_candidates(ir)
    assert candidates["ptr"]["values"] == {0, 1}
    assert candidates["*ptr"]["values"] == {0, 3, 4, 5, 255}
    assert _pointer_column_key("ptr", "@ptr[0]", ir) == "*ptr"
    baseline = load_baseline(ROOT / "config" / "baselines" /
                             "psd-rebuild" / "1.0.yaml")
    nested_true = next(item for item in derive_obligations(ir, baseline)
                       if item.oid == "value:T")
    witness = solve_obligation(ir, nested_true, baseline)
    assert witness.status == "SAT"
    assert witness.assignment["ptr"] == 1


def test_issue6_const_table_branch_uses_driver_indexes_as_proof_domain():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    derived = ValueOrigin(
        kind="const_table_field", driver="index",
        table_values={"0": 255, "1": 46},
    )
    ir = FunctionIR(
        name="table_target", file="target.c", line=1, ret_type="void",
        params=[Param("index", "unsigned char", type_info=info)],
        branches=[Branch(
            bid="b0", kind="if", line=2,
            atoms=[Atom("derived", "unsigned char", "!=", 255,
                        None, "derived != 255", type_info=info)],
        )],
        control_vars=[
            ControlVar("index", "index", "param", type_info=info),
            ControlVar("derived", "derived", "derived", type_info=info,
                       value_origin=derived),
        ],
    )
    from ut_agent.generation import engine
    domains, _fixed = engine._generic_inputs(ir)
    assert domains["index"] == [0, 1]


def test_local_from_global_candidates_follow_external_driver():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="global_alias_target", file="target.c", line=1, ret_type="void",
        globals_used=["source"],
        global_objects=[GlobalObject(name="source", read=True)],
        branches=[Branch(
            bid="b0", kind="if", line=2,
            atoms=[Atom("derived", "unsigned char", "==", 1,
                        None, "derived == 1", type_info=info)],
        )],
        control_vars=[ControlVar(
            "derived", "derived", "local_from_global", type_info=info,
            value_origin=ValueOrigin(kind="local_from_global", driver="source"),
        )],
    )
    from ut_agent.generation import engine

    domains, fixed = engine._generic_inputs(ir)
    assert "source" in domains
    assert "derived" not in domains
    assert engine._control_env({**fixed, "source": 7, "derived": 1}, ir)[
        "derived"
    ] == 7


def test_local_from_global_resolves_before_dependent_local_evaluations():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    # Put dependent_local BEFORE derived_global in control_vars
    ir = FunctionIR(
        name="order_dependent_target", file="target.c", line=1, ret_type="void",
        globals_used=["source"],
        global_objects=[GlobalObject(name="source", read=True)],
        branches=[
            Branch(
                bid="b0", kind="if", line=2,
                atoms=[Atom("derived_global", "unsigned char", "==", 1,
                            None, "derived_global == 1", type_info=info)],
            ),
            Branch(
                bid="b1", kind="if", line=4,
                atoms=[Atom("dependent_local", "unsigned char", "!=", 0,
                            None, "dependent_local != 0", type_info=info)],
            ),
        ],
        control_vars=[
            ControlVar(
                "dependent_local", "dependent_local", "local", type_info=info,
                value_origin=ValueOrigin(kind="constant", expression="0"),
            ),
            ControlVar(
                "derived_global", "derived_global", "local_from_global", type_info=info,
                value_origin=ValueOrigin(kind="local_from_global", driver="source"),
            ),
        ],
    )
    from ut_agent.generation import engine

    env = engine._control_env({"source": 42}, ir)
    assert env["derived_global"] == 42


def test_exact_dynamic_global_path_precedes_normalized_alias():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="dynamic_global_target", file="target.c", line=1, ret_type="void",
        globals_used=["array"],
        global_objects=[GlobalObject(
            name="array", read=True, array_sizes=[2],
            index_drivers=["index"], field_paths=["field"],
        )],
        branches=[Branch(
            bid="b0", kind="if", line=2,
            atoms=[Atom("array[ index ].field", "unsigned char", "!=", 3,
                        None, "array[index].field != 3", type_info=info)],
        )],
        control_vars=[ControlVar(
            "field", "array[ index ].field", "global", type_info=info,
        )],
    )
    from ut_agent.generation import engine

    values = {
        "global:array[0].field": 0,
        "global:array[1].field": 0,
        "array[ index ].field": 3,
        "array[index].field": 0,
    }
    env = engine._control_env(values, ir)
    assert engine.evaluate_atom(ir.branches[0].atoms[0], env) is False


def test_dynamic_global_value_projects_to_selected_winams_cell():
    ir = FunctionIR(
        name="dynamic_projection_target", file="target.c", line=1, ret_type="void",
        global_objects=[GlobalObject(
            name="array", read=True, array_sizes=[2],
            index_drivers=["index"], field_paths=["field"],
        )],
    )
    values = {
        "index": 1,
        "global:array[0].field": 0,
        "global:array[1].field": 0,
        "array[ index ].field": 7,
    }
    assert _intent_value(
        values, "target.c/array[1].field", "global:array[1].field", ir=ir,
    ) == 7


def test_issue12_table_index_coverage_comes_from_explicit_runtime_class():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    derived = ValueOrigin(
        kind="const_table_field", driver="index",
        table_values={"0": 255, "1": 46},
    )
    ir = FunctionIR(
        name="table_policy_target", file="target.c", line=1, ret_type="void",
        params=[Param("index", "unsigned char", type_info=info)],
        branches=[Branch(
            bid="b0", kind="if", line=2,
            atoms=[Atom("derived", "unsigned char", "<", 300,
                        None, "derived < 300", type_info=info)],
        )],
        control_vars=[
            ControlVar("index", "index", "param", type_info=info),
            ControlVar("derived", "derived", "derived", type_info=info,
                       value_origin=derived),
        ],
    )
    from ut_agent.generation import engine
    obligation = SimpleNamespace(kind="branch", outcome=True, branch_id="b0")
    all_indexes = load_baseline(
        ROOT / "config" / "baselines" / "psd-rebuild" / "1.1.yaml"
    )
    selected_only = SimpleNamespace(array_policy={
        "comparison_classes": {"table_array": {"index_coverage": "selected"}},
    })
    assert {item["index"] for item in engine._coverage_variants(
        ir, all_indexes, obligation, {"index": 0},
    )} == {0, 1}
    assert engine._coverage_variants(
        ir, selected_only, obligation, {"index": 0},
    ) == ({"index": 0},)


def test_issue6_loop_policy_emits_internal_loop_entry_obligations():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="loop_target", file="target.c", line=1, ret_type="void",
        branches=[Branch(
            bid="loop", kind="for", line=2,
            atoms=[Atom("index", "unsigned char", "<", 3,
                        None, "3 > index", type_info=info)],
        )],
        control_vars=[ControlVar("index", "index", "local", type_info=info)],
    )
    baseline = load_baseline(ROOT / "config" / "baselines" /
                             "psd-rebuild" / "1.0.yaml")
    obligations = derive_obligations(ir, baseline)
    assert [(item.kind, item.branch_id, item.outcome) for item in obligations] == [
        ("loop", "loop", True),
    ]
