"""Architecture gates for the FunctionIR v3 migration in Issue #1."""
from __future__ import annotations

import json
from pathlib import Path

from ut_agent.cases.boundary import control_candidates
from ut_agent.ir import Atom, Branch, CallSite, ControlVar, FunctionIR, Param, TypeInfo
from ut_agent.rules.engine import evaluate_atom, evaluate_branch
from ut_agent.stub.generate import render_stub_c

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ut_agent"
EXTRACTOR = ROOT / "tooling" / "ut-clang-extract"


def _python_sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_python_pipeline_has_no_c_parser_or_core_extension_reads():
    text = "\n".join(path.read_text(encoding="utf-8") for path in _python_sources())
    assert "clang.cindex" not in text
    assert "extensions.get" not in text
    assert "infer_branch_nesting" not in text
    assert "is_scalar_type" not in text
    assert "selected_global_writes" not in text
    assert "_BITMASK" not in text
    assert "_MEMORY_HELPER" not in text


def test_rules_do_not_reopen_source_or_own_target_column_spelling():
    engine = (SRC / "rules" / "engine.py").read_text(encoding="utf-8")
    learner = (SRC / "rules" / "infer.py").read_text(encoding="utf-8")
    corpus = (SRC / "rules" / "corpus.py").read_text(encoding="utf-8")
    model = (SRC / "rules" / "model.py").read_text(encoding="utf-8")
    assert "AMSTB_" not in engine
    assert "PTROUT" not in engine
    assert "AMIN_return" not in engine
    assert "CALLCNT" not in engine
    assert "read_text" not in learner
    assert "rglob" not in learner
    assert "source_macro" not in learner
    assert "_source_text" not in learner
    assert "ClangExtractor" not in corpus
    assert "read_text" not in corpus
    assert "rglob" not in corpus
    assert "input_columns" not in model
    assert "output_columns" not in model
    assert "stub_declarations" not in model


def test_parser_owns_corpus_extraction_orchestration():
    collector = (SRC / "parser" / "rule_corpus.py").read_text(encoding="utf-8")
    assert "ClangExtractor" in collector
    assert "make_compile_context" in collector


def test_extractor_pass_boundary_and_v3_schema_are_present():
    schema = json.loads(
        (ROOT / "docs" / "function-ir-v3.schema.json").read_text(encoding="utf-8")
    )
    assert schema["$id"].endswith("function-ir-v3.json")
    assert schema["properties"]["schema_version"]["const"] == 3
    cmake = (EXTRACTOR / "CMakeLists.txt").read_text(encoding="utf-8")
    main = (EXTRACTOR / "main.cpp").read_text(encoding="utf-8")
    assert "passes/type_facts.cpp" in cmake
    assert "passes/contract_validation.cpp" in cmake
    assert '#include "passes/type_facts.h"' in main
    assert '#include "passes/contract_validation.h"' in main
    assert "parameterWriteEffects()" in main
    assert '"order", Effect.Order' in main


def test_flow_package_has_no_python_producer():
    flow = SRC / "flow"
    assert not any(flow.glob("*.py"))


def test_rules_consume_typed_mask_and_domain_facts():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="target", file="target.c", line=1, ret_type="void",
        params=[Param("flags", "unsigned char", type_info=info)],
        branches=[Branch(
            bid="b0", kind="if", line=2,
            atoms=[Atom(
                "flags", "unsigned char", "!=", 0, None,
                "(flags & 0x30) != 0", mask=0x30, type_info=info,
            )],
            condition_tree={"kind": "atom", "index": 0},
        )],
        control_vars=[ControlVar(
            "flags", "flags", "param", var_type="unsigned char",
            type_info=info, branch_ids=["b0"],
        )],
    )
    assert evaluate_atom(ir.branches[0].atoms[0], {"flags": 0x30})
    assert control_candidates(ir)["flags"]["values"]


def test_rules_preserve_logical_not_in_condition_tree():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    branch = Branch(
        bid="b0", kind="if", line=2,
        atoms=[Atom("flags", "unsigned char", "==", 0, None,
                    "flags == 0", type_info=info)],
        condition_tree={
            "kind": "not",
            "child": {"kind": "atom", "index": 0},
        },
    )
    assert evaluate_branch(branch, {"flags": 1})
    assert not evaluate_branch(branch, {"flags": 0})


def test_pointer_type_fact_keeps_pointee_domain_for_stub_generation():
    pointee = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    pointer = TypeInfo(
        canonical_type="unsigned char *", kind="pointer", pointer_depth=1,
        pointee_type="unsigned char", pointee_info=pointee,
    )
    ir = FunctionIR(
        name="target", file="target.c", line=1, ret_type="void",
        calls=[CallSite(
            order=0, callee="dep", line=2,
            params=[Param("out", "unsigned char *", is_ptr=True,
                          is_written=True, type_info=pointer)],
        )],
    )
    text = render_stub_c(ir)
    assert "static volatile unsigned char PTROUT00_dep" in text
