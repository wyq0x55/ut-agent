"""确定性规则引擎：约束、审批、manifest 与 DMA 学习样本。"""
from __future__ import annotations

import json

import pytest

from conftest import ROOT
from ut_agent import cli
from ut_agent.ir import (
    Atom, Branch, CallSite, ControlVar, Effect, FieldAccess, FunctionIR,
    GlobalObject, Param, RecordLayoutField, TypeInfo,
)
from ut_agent.rules import (
    NEEDS_REVIEW, UNSUPPORTED, VALIDATED, Rule, RulePack, evaluate_atom,
    approve_rule_pack, generate_intents, load_rule_pack,
    review_rule_pack,
)
from ut_agent.winams.csv_render import render_intents_csv
from ut_agent.winams.rule_infer import infer_rule_pack
from ut_agent.parser import ClangExtractor, default_clang_extractor, make_compile_context
from ut_agent.cases.boundary import control_candidates
from ut_agent.rules.semantic import global_key


def _branch_ir(*, ret_type: str = "void") -> FunctionIR:
    type_info = TypeInfo(
        canonical_type="uint8", kind="integer", bit_width=8, signed=False,
        min_value=0, max_value=255,
    )
    return FunctionIR(
        name="target", file="target.c", line=1, ret_type=ret_type,
        params=[Param("value", "uint8", type_info=type_info)],
        branches=[Branch(
            bid="B01", kind="if", line=2, cond_text="value == 1",
            atoms=[Atom("value", "uint8", "==", 1, None, "value == 1",
                        type_info=type_info)],
        )],
        control_vars=[ControlVar(
            "value", "value", "param", var_type="uint8", type_info=type_info,
        )],
    )


def test_expression_evaluator_supports_comparison_and_bitmask():
    assert evaluate_atom(
        Atom("value", "uint8", "==", 1, None, "value == 1"), {"value": 1}
    )
    masked = Atom(
        "flags", "uint32", "!=", 0, None,
        "0 != (flags & 0x30)", mask=0x30,
    )
    assert evaluate_atom(masked, {"flags": 0x30})
    assert not evaluate_atom(masked, {"flags": 0})


def test_generic_solver_proves_true_and_false_without_cartesian_dump():
    result = generate_intents(_branch_ir())
    assert result.status == VALIDATED
    assert len(result.intents) == 2
    assert {item.obligation.outcome for item in result.intents} == {True, False}
    assert all(item.validation.valid for item in result.intents)


def test_generic_solver_rejects_high_cardinality_before_product_materialization():
    names = [f"state_{index}" for index in range(13)]
    ir = FunctionIR(
        name="wide_target", file="target.c", line=1, ret_type="void",
        branches=[Branch(
            bid="B01", kind="if", line=2, connective="&&",
            cond_text=" && ".join(f"{name} == 1" for name in names),
            atoms=[Atom(name, "uint8", "==", 1, None, f"{name} == 1")
                   for name in names],
        )],
        control_vars=[ControlVar(name, name, "global", var_type="uint8")
                      for name in names],
    )
    result = generate_intents(ir)
    assert result.status == UNSUPPORTED
    assert result.issues == ("没有可证明的测试用例",)


def test_csv_prefers_semantic_expanded_condition_text():
    ir = _branch_ir()
    ir.branches[0].cond_text = "value == MODE_ON"
    ir.branches[0].cond_text_spelling = "value == MODE_ON"
    ir.branches[0].cond_text_expanded = "value == 1"
    result = generate_intents(ir)

    text = render_intents_csv(ir, result)
    assert ";$L$,if ( value == 1 )" in text
    assert ";$L$,value == MODE_ON" not in text


def test_missing_return_oracle_is_not_reported_as_validated():
    result = generate_intents(_branch_ir(ret_type="uint8"))
    assert result.status == NEEDS_REVIEW
    assert any("oracle" in error for error in result.issues)
    text = render_intents_csv(_branch_ir(ret_type="uint8"), result)
    assert "$L$,TRUE" in text and "$L$,FALSE" in text
    assert ",0x0" not in text


def test_pointer_guard_does_not_empty_generic_solver_domain():
    ir = FunctionIR(
        name="pointer_target", file="target.c", line=1, ret_type="void",
        params=[Param("ptr", "uint8 *", is_ptr=True, is_written=True)],
        branches=[
            Branch(
                bid="B00", kind="if", line=2, cond_text="ptr != NULL",
                atoms=[Atom("ptr", "uint8 *", "!=", None, "NULL", "ptr != NULL")],
            ),
            Branch(
                bid="B01", kind="if", line=3, cond_text="*ptr == 1",
                atoms=[Atom("*ptr", "uint8", "==", 1, None, "*ptr == 1")],
                parent_bid="B00",
            ),
        ],
        control_vars=[
            ControlVar("ptr", "ptr", "param", var_type="uint8 *"),
            ControlVar("*ptr", "*ptr", "param", var_type="uint8"),
        ],
    )
    result = generate_intents(ir)
    assert result.status == NEEDS_REVIEW
    assert len(result.intents) == 2
    assert {item.obligation.branch_id for item in result.intents} == {"B00", "B01"}
    assert all(all(value >= 0 for value in item.inputs.values()
                   if isinstance(value, int)) for item in result.intents)


def test_generic_stub_slots_are_materialized_for_rendering():
    ir = FunctionIR(
        name="stub_target", file="target.c", line=1, ret_type="void",
        params=[Param("value", "uint8", type_info=TypeInfo(
            canonical_type="uint8", kind="integer", bit_width=8,
            signed=False, min_value=0, max_value=255,
        ))],
        branches=[Branch(
            bid="B01", kind="if", line=2, cond_text="value == 1",
            atoms=[Atom("value", "uint8", "==", 1, None, "value == 1",
                        type_info=TypeInfo(
                            canonical_type="uint8", kind="integer", bit_width=8,
                            signed=False, min_value=0, max_value=255,
                        ))],
        )],
        control_vars=[ControlVar(
            "value", "value", "param", var_type="uint8", type_info=TypeInfo(
                canonical_type="uint8", kind="integer", bit_width=8,
                signed=False, min_value=0, max_value=255,
            ),
        )],
        calls=[CallSite(
            order=0, callee="stub_api", line=3,
            params=[
                Param("data", "uint8 *", is_ptr=True, type_info=TypeInfo(
                    canonical_type="uint8 *", kind="pointer", pointer_depth=1,
                    pointee_type="uint8",
                )),
                Param("size", "uint8", type_info=TypeInfo(
                    canonical_type="uint8", kind="integer", bit_width=8,
                    signed=False, min_value=0, max_value=255,
                )),
            ],
            ret_type="uint8", max_occurrences=2, return_used=True,
        )],
    )
    result = generate_intents(ir)
    assert result.status == VALIDATED
    text = render_intents_csv(ir, result)
    for suffix in (
        "CALLCNT_stub_api",
        "PTROUT00_stub_api[0]", "PTROUT00_stub_api[1]",
        "ARG01_stub_api[0]", "ARG01_stub_api[1]",
        "AMIN_return[0]", "AMIN_return[1]",
    ):
        assert f"AMSTB_SrcFile.c/AMSTB_stub_api@{suffix}" in text


def test_embedded_unsigned_alias_uses_unsigned_domain():
    ir = _branch_ir()
    ir.params[0].type = "u1"
    ir.control_vars[0].var_type = "u1"
    ir.params[0].type_info = TypeInfo(
        canonical_type="u1", kind="integer", bit_width=8, signed=False,
        min_value=0, max_value=255,
    )
    ir.control_vars[0].type_info = ir.params[0].type_info
    values = control_candidates(ir)["value"]["values"]
    assert min(values) == 0
    assert max(values) == 255


def test_unknown_local_source_is_unsupported_instead_of_becoming_an_input():
    ir = _branch_ir()
    ir.control_vars[0].source = "local"
    result = generate_intents(ir)
    assert result.status == UNSUPPORTED
    assert "来源不可设定" in result.issues[0]


def test_record_storage_oracle_uses_typed_layout_without_project_names():
    layout = [
        RecordLayoutField("bits.b0", 0, 1, True, "bits", 0, 8),
        RecordLayoutField("bits.b1", 1, 1, True, "bits", 0, 8),
        RecordLayoutField("byte", 0, 8, False, "byte", 0, 8),
    ]
    ir = FunctionIR(
        name="record_target", file="record.c", line=1, ret_type="void",
        global_objects=[GlobalObject(
            name="state", write=True,
            field_paths=["bits.b0", "bits.b1", "byte"],
            field_accesses=[
                FieldAccess("bits.b0", write=True),
                FieldAccess("bits.b1", write=True),
            ], record_layout=layout,
        )],
        global_write_effects=[
            Effect(path="state.bits.b0", constant_value=1),
            Effect(path="state.bits.b1", constant_value=0),
        ],
    )
    result = generate_intents(ir)
    assert result.status == VALIDATED
    assert result.intents[0].expected[global_key("state", field="byte")] == 1
    assert all(not key.startswith("AMSTB_") for key in result.intents[0].expected)


def test_unknown_global_output_is_needs_review_not_zero():
    ir = FunctionIR(
        name="unknown_global", file="record.c", line=1, ret_type="void",
        global_objects=[GlobalObject(name="state", write=True)],
    )
    result = generate_intents(ir)
    assert result.status == NEEDS_REVIEW
    assert any("oracle" in issue for issue in result.issues)


def test_generation_manifest_is_deterministic():
    first = json.dumps(generate_intents(_branch_ir()).to_dict(), sort_keys=True)
    second = json.dumps(generate_intents(_branch_ir()).to_dict(), sort_keys=True)
    assert first == second


def test_candidate_rule_never_participates_at_runtime():
    candidate = Rule(
        "project.target.scenarios", "candidate", {"function": "target"},
        {"kind": "scenario_matrix"}, {"scenarios": []}, 1,
        ("golden",), {},
    )
    result = generate_intents(_branch_ir(), RulePack("candidate", 1, (candidate,)))
    assert result.rule_pack == "candidate"
    assert all(trace.rule_id == "builtin.compare"
               for intent in result.intents for trace in intent.trace)


def test_rule_review_reports_candidates_without_activating_them(tmp_path):
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps({
        "name": "candidate", "version": 1,
        "rules": [{
            "id": "project.target.candidate", "status": "candidate",
            "scope": {"function": "target"}, "match": {"kind": "state"},
            "action": {"strategy": "valid-invalid"}, "priority": 20,
            "evidence": ["golden"], "approval": {},
        }],
    }), encoding="utf-8")
    report = review_rule_pack(path)
    assert report["counts"] == {"candidate": 1, "approved": 0, "rejected": 0}
    assert report["rules"][0]["approval"] is False


def test_approval_creates_new_pack_and_preserves_candidate(tmp_path):
    source = tmp_path / "candidate.json"
    source.write_text(json.dumps({
        "name": "candidate", "version": 1,
        "rules": [{
            "id": "r1", "status": "candidate", "scope": {"function": "*"},
            "match": {"kind": "state"}, "action": {}, "priority": 20,
            "evidence": ["golden"], "approval": {},
        }],
    }), encoding="utf-8")
    approved = tmp_path / "approved.json"
    approve_rule_pack(source, approved, authority="reviewer", reason="checked")
    original = json.loads(source.read_text(encoding="utf-8"))
    result = json.loads(approved.read_text(encoding="utf-8"))
    assert original["rules"][0]["status"] == "candidate"
    assert result["rules"][0]["status"] == "approved"
    assert result["rules"][0]["approval"]["authority"] == "reviewer"


def test_approved_scenario_rule_is_validated_and_rendered_without_defaults(tmp_path):
    raw = {
        "name": "approved", "version": 1,
        "rules": [{
            "id": "project.target.scenarios", "status": "approved",
            "scope": {"function": "target"},
            "match": {"kind": "scenario_matrix"}, "priority": 1,
            "action": {
                "input_columns": ["value"], "output_columns": [],
                "scenarios": [
                    {"branch_index": 0, "outcome": True, "inputs": {"value": 1},
                     "expected": {}},
                    {"branch_index": 0, "outcome": False, "inputs": {"value": 0},
                     "expected": {}},
                ],
            },
            "evidence": ["reviewed-golden"],
            "approval": {"authority": "unit-test", "reason": "reviewed"},
        }],
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    result = generate_intents(_branch_ir(), load_rule_pack(path))
    assert result.status == VALIDATED
    text = render_intents_csv(_branch_ir(), result)
    assert ";$L$,TRUE\r\n,0x1" in text
    assert ";$L$,FALSE\r\n,0x0" in text
    assert "omitted_unvalidated" not in text


@pytest.mark.parametrize("function,rows", [
    ("Dma_Csih2Receive", 3),
    ("Dma_Csih2Send", 5),
    ("Dma_Error", 3),
    ("Os_Isr_f_vog_dma_interrupt_DMA05", 1),
    ("p_vog_dma_init", 3),
])
def test_dma_golden_infers_reviewable_scenarios(function, rows):
    golden = ROOT / "examples" / "golden" / "n-o2602-mvc-234" / "dma" / function / "TestCsv.csv"
    inferred = infer_rule_pack(
        FunctionIR(function, "Dma.c", 1, "void"), golden,
    )
    rule = inferred["rules"][0]
    assert rule["status"] == "candidate"
    assert rule["approval"] == {}
    assert len(rule["action"]["scenarios"]) == rows
    assert rule["action"]["input_columns"]
    assert rule["evidence"][0].startswith("sha256:")


def test_rules_infer_cli_writes_candidate_pack(tmp_path):
    source = tmp_path / "target.c"
    source.write_text(
        "typedef unsigned char uint8;\n"
        "void target(uint8 value) { if (value == 1) { } }\n",
        encoding="utf-8",
    )
    golden = tmp_path / "target.csv"
    golden.write_bytes(
        ('mod,"target","target",1,0,,,,CPP,,,"",0\r\n'
         '#COMMENT,"value"\r\n'
         ';$L$,if (value == 1)\r\n'
         ';$L$,TRUE\r\n,1\r\n'
         ';$L$,FALSE\r\n,0\r\n').encode("cp932")
    )
    output = tmp_path / "rules.json"
    assert cli.main([
        "rules", "infer", str(source), "-f", "target",
        "--golden", str(golden), "-o", str(output),
    ]) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["rules"][0]["status"] == "candidate"
    assert len(raw["rules"][0]["action"]["scenarios"]) == 2


def test_rules_collect_cli_batches_samples_with_explicit_include_dirs(tmp_path):
    soft = tmp_path / "Soft" / "src"
    source_dir = soft / "mod"
    source_dir.mkdir(parents=True)
    source = source_dir / "target.c"
    source.write_text(
        "typedef unsigned char uint8;\n"
        "void target(uint8 value) { if (value == 1) { } }\n",
        encoding="utf-8",
    )
    winams = tmp_path / "winAMS" / "src" / "mod" / "target.c" / "target" / "TestCsv"
    winams.mkdir(parents=True)
    golden = winams / "target.csv"
    golden.write_bytes(
        ('mod,"target","target",1,0,,,,CPP,,,"",0\r\n'
         '#COMMENT,"value"\r\n'
         ';$L$,if (value == 1)\r\n'
         ';$L$,TRUE\r\n,1\r\n'
         ';$L$,FALSE\r\n,0\r\n').encode("cp932")
    )
    output = tmp_path / "corpus.json"
    candidate = tmp_path / "candidate.json"
    assert cli.main([
        "rules", "collect", str(tmp_path / "winAMS" / "src"),
        "--source-root", str(soft), "-I", str(soft),
        "-o", str(output), "--candidate-pack", str(candidate),
    ]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["counts"] == {
        "samples": 1, "inferred": 1, "unsupported": 0,
        "patterns": 1, "derived_rules": 1, "candidate_rules": 3,
    }
    assert report["candidate_pack"]["profile"] == {
        "base_profile": "PSD再構築",
        "profile_version": "PSD再構築-v1",
        "mcdc_enabled": True,
        "approved_exceptions": [],
    }
    assert report["candidate_pack"]["samples_are_evidence_only"] is True


def test_inference_does_not_guess_local_bitmask_binding(tmp_path):
    golden = tmp_path / "mask.csv"
    golden.write_bytes(
        ('mod,"target","target",1,0,,,,CPP,,,"",0\r\n'
         '#COMMENT,"U4L_REG"\r\n'
         ';$L$,if (0 != (local & 0x30))\r\n'
         ';$L$,TRUE\r\n,0x30\r\n'
         ';$L$,FALSE\r\n,0x0\r\n').encode("cp932")
    )
    ir = FunctionIR(
        "target", "target.c", 1, "void",
        branches=[Branch(
            "B01", "if", 2, cond_text="0 != (local & 0x30)",
            atoms=[Atom("local & 0x30", "uint32", "!=", 0, None,
                        "0 != (local & 0x30)")],
        )],
    )
    action = infer_rule_pack(ir, golden)["rules"][0]["action"]
    assert action["bindings"] == {}
    assert "local" not in action["scenarios"][0]["inputs"]


def test_inference_keeps_mcdc_combination_labels_under_current_branch(tmp_path):
    golden = tmp_path / "mcdc.csv"
    golden.write_bytes(
        ('mod,"target","target",1,0,,,,CPP,,,"",0\r\n'
         '#COMMENT,"value"\r\n'
         ';$L$,if (value == 1)\r\n'
         ';$L$,T||F => T\r\n,1\r\n'
         ';$L$,組合せ(F||F => F①)\r\n,0\r\n').encode("cp932")
    )
    action = infer_rule_pack(_branch_ir(), golden)["rules"][0]["action"]
    scenarios = action["scenarios"]
    assert [item["branch_index"] for item in scenarios] == [0, 0]
    assert [item["outcome"] for item in scenarios] == [True, False]
    assert action["rule_evidence"]["mcdc_combinations"]


def test_evaluate_atom_supports_source_bound_variable_comparison():
    atom = Atom(
        "cfg[ id ].limit", "uint8", ">", None, None,
        "cfg[ id ].limit > count[ id ]", right="count[id]",
    )
    assert evaluate_atom(atom, {
        "cfg[id].limit": 3,
        "count[id]": 1,
    })
    assert not evaluate_atom(atom, {
        "cfg[id].limit": 3,
        "count[id]": 4,
    })
    assert evaluate_atom(
        atom,
        {"cfg[id].limit": 3, "count[id]": 4},
        {"count[id]": 1},
    )


def test_inference_preserves_switch_case_semantics(tmp_path):
    golden = tmp_path / "switch.csv"
    golden.write_bytes(
        ('mod,"target","target",1,0,,,,CPP,,,"",0\r\n'
         '#COMMENT,"value"\r\n'
         ';$L$,switch (value)\r\n'
         ';$L$,case 0:\r\n,0\r\n'
         ';$L$,組合せ(case 0:(1))\r\n,0\r\n'
         ';$L$,default:\r\n,2\r\n'
         ';$L$,if (value == 2)\r\n'
         ';$L$,TRUE\r\n,2\r\n').encode("cp932")
    )
    scenarios = infer_rule_pack(FunctionIR(
        "target", "target.c", 1, "void",
        branches=[
            Branch("B01", "switch", 2, cond_text="switch (value)"),
            Branch("B02", "if", 5, cond_text="value == 2",
                   atoms=[Atom("value", "uint8", "==", 2, None, "value == 2")]),
        ],
    ), golden)[
        "rules"
    ][0]["action"]["scenarios"]
    assert [item["kind"] for item in scenarios] == ["case", "case", "case", "scenario"]
    assert scenarios[1]["case_label"] == "case 0:"
    assert scenarios[2]["case_label"] == "default:"
    assert scenarios[0]["kind"] == "case"
    assert scenarios[1]["label"] == "組合せ(case 0:(1))"
    assert [item["branch_index"] for item in scenarios] == [0, 0, 0, 1]


def test_inference_does_not_bind_source_expression_to_golden_column(tmp_path):
    source = tmp_path / "target.c"
    source.write_text("#define IDX 3\n", encoding="utf-8")
    ir = FunctionIR(
        "target", str(source), 1, "void",
        branches=[Branch(
            "B01", "if", 2, cond_text="state[IDX] == 1",
            atoms=[Atom("state[ IDX ]", "uint8", "==", 1, None,
                        "state[ IDX ] == 1")],
        )],
    )
    golden = tmp_path / "target.csv"
    golden.write_bytes(
        ('mod,"target","target",1,0,,,,CPP,,,"",0\r\n'
         '#COMMENT,"mod/state[3]"\r\n'
         ';$L$,state[IDX] == 1\r\n'
         ';$L$,TRUE\r\n,1\r\n').encode("cp932")
    )
    action = infer_rule_pack(ir, golden)["rules"][0]["action"]
    assert action["bindings"] == {}


def test_clang_recovers_macro_condition_and_mask_from_source_line(tmp_path):
    source = tmp_path / "mask.c"
    source.write_text(
        "typedef unsigned int uint32;\n"
        "#define MASK 0x30U\n"
        "void target(uint32 flags) { if (0U != (flags & MASK)) { } }\n",
        encoding="utf-8",
    )
    context = make_compile_context([source])
    ir = ClangExtractor(default_clang_extractor()).extract(
        context, "target", cwd=tmp_path
    )
    assert len(ir.branches) == 1
    atom = ir.branches[0].atoms[0]
    assert atom.boundary == 0
    assert atom.mask == 0x30
    assert "flags" in atom.var
