import json

from ut_agent.ir import Atom, Branch, ControlVar, FunctionIR, Param
from ut_agent.rules import generate_intents
from ut_agent.rules.compress import _branch_family, _digest, compress_corpus, compress_corpora
from ut_agent.rules.pack import Rule, RulePack, approve_rule_pack, load_rule_pack, review_rule_pack


def _corpus(function: str, project: str = "P"):
    return {
        "kind": "ut-agent-rule-corpus",
        "roots": {"winams": project},
        "candidate_pack": {
            "profile": {"profile_version": "PSD再構築-v1"},
            "rules": [],
        },
        "samples": [{
            "function": function,
            "source_rel": f"{function}.c",
            "source_facts": {"pattern": {"shape": {"branches": [{
                "kind": "if", "connective": "single", "atoms": [{
                    "op": "==", "boundary_class": "enum-or-macro",
                    "masked": False, "mask_width": None,
                }],
            }]}}},
        }],
    }


def test_single_project_family_is_not_cross_project():
    result = compress_corpus({**_corpus("A"), "roots": {"winams": "N-O2504"}},
                             project_id="N-O2504")
    assert result["counts"] == {
        "families": 1, "project_specific": 1,
        "cross_function": 0, "cross_project": 0, "profile_rules": 0,
    }
    assert result["families"][0]["classification"] == "PROJECT_SPECIFIC"


def test_two_projects_promote_same_family():
    result = compress_corpora(
        [_corpus("A"), _corpus("B")],
        project_ids=["N-O2504", "N-O2605"],
    )
    assert result["counts"]["cross_project"] == 1
    assert result["families"][0]["project_count"] == 2


def test_approved_semantic_family_is_traced_during_generation():
    family = _branch_family({
        "kind": "if", "connective": "single",
        "atoms": [{"op": "==", "boundary_class": "one",
                   "masked": False, "mask_width": None}],
    })
    rule_id = "semantic.test-family"
    pack = RulePack("test", 1, (
        Rule(rule_id, "approved", {"function": "*"},
             {"kind": "semantic_family", "family_id": f"family.{_digest(family)}"},
             {"strategy": "instantiate-from-ast"}, 40,
             approval={"authority": "test", "reason": "test"}),
    ))
    ir = FunctionIR(
        "target", "target.c", 1, "void",
        params=[Param("value", "uint8")],
        branches=[Branch("B01", "if", 2, "target.c", "value == 1",
                         [Atom("value", "uint8", "==", 1, None,
                               "value == 1")])],
        control_vars=[ControlVar("value", "value", "param", var_type="uint8")],
    )
    result = generate_intents(ir, pack)
    assert result.intents
    assert any(trace.rule_id == rule_id for trace in result.intents[0].trace)


def test_compressed_candidate_pack_can_be_approved_and_loaded(tmp_path):
    source = tmp_path / "compressed.json"
    source.write_text(json.dumps({
        "name": "compressed-report",
        "candidate_pack": {
            "name": "formal-pack",
            "version": 1,
            "rules": [{
                "id": "semantic.example", "status": "candidate",
                "scope": {"function": "*"},
                "match": {"kind": "semantic_family", "family_id": "family.x"},
                "action": {"strategy": "instantiate-from-ast"},
                "priority": 40, "evidence": ["projects:2"],
            }],
        },
    }), encoding="utf-8")
    output = tmp_path / "approved.json"
    approve_rule_pack(source, output, authority="test", reason="evidence",
                      rule_ids={"semantic.example"})
    assert review_rule_pack(output)["counts"] == {
        "candidate": 0, "approved": 1, "rejected": 0,
    }
    assert load_rule_pack(output).approved("any", "semantic_family")[0].rule_id == "semantic.example"
