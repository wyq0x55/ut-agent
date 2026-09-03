"""Issue #6 project-corpus manifest and semantic comparison gates."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ut_agent.baseline import load_baseline
from ut_agent.learning.compare import compare_testcsv
from ut_agent.learning import label_kind, normalize_golden_csv, normalize_label
from ut_agent.generation.boundary import control_candidates
from ut_agent.generation.obligation import derive_obligations
from ut_agent.generation.solver import solve_obligation
from ut_agent.ir import Atom, Branch, ControlVar, FunctionIR, Param, TypeInfo, ValueOrigin
from ut_agent.project import load_manifest
from ut_agent.reporting import (
    STANDARD_GAP_CATEGORIES,
    compare_function_semantics,
    load_corpus_manifest,
    validate_corpus_paths,
)
from ut_agent.targets.winams.csv import _pointer_column_key


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
    assert context.baseline_ref == "psd-rebuild@1.0"
    assert "baseline" not in manifest.to_dict()["project"]


def test_issue6_baseline_keeps_source_mapped_approved_rules():
    baseline = load_baseline(ROOT / "config" / "baselines" / "psd-rebuild" / "1.0.yaml")
    assert len(baseline.rules) == 8
    assert {item["status"] for item in baseline.rules} == {"approved"}
    assert {item["rule_id"] for item in baseline.rules} >= {
        "psd.0-2.typed-domain", "psd.4.mcdc", "psd.6.order",
    }


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
