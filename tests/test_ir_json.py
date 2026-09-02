"""FunctionIR v3 contract tests and the public C evidence fixtures."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ut_agent.ir import (
    Atom,
    Branch,
    CallSite,
    ControlVar,
    FunctionIR,
    Param,
    Provenance,
    SourceLocation,
    TypeInfo,
)
from ut_agent.parser.ir_json import (
    FunctionIRSchemaError,
    document_to_function_ir,
    serialize_document,
    validate_document,
)
from ut_agent.parser import ClangExtractor, ClangExtractorError, default_clang_extractor, make_compile_context


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "function_ir_v2"


def _provenance(kind: str = "FixtureNode") -> Provenance:
    location = SourceLocation(
        file=str(FIXTURE_ROOT / "sample.c"), line=9, column=5,
        offset=100, end_offset=120,
    )
    return Provenance(
        spelling=location,
        expansion=location,
        macro_stack=[],
        ast_kind=kind,
    )


def _document() -> dict:
    type_info = TypeInfo(
        canonical_type="int", kind="integer", bit_width=32, signed=True,
        min_value=-(2**31), max_value=2**31 - 1,
    )
    atom = Atom(
        var="value", var_type="int", op="==", boundary=1,
        boundary_name=None, text="value == 1", mask=None,
        cond_text_spelling="value == MODE_ON",
        cond_text_expanded="value == 1",
        type_spelling="int", canonical_type="int", type_info=type_info,
        provenance=_provenance("BinaryOperator"),
    )
    branch = Branch(
        bid="B01", kind="if", line=9, file=str(FIXTURE_ROOT / "sample.c"),
        cond_text="value == 1", atoms=[atom],
        cond_text_spelling="value == MODE_ON", cond_text_expanded="value == 1",
        provenance=_provenance("IfStmt"),
    )
    ir = FunctionIR(
        name="target", file=str(FIXTURE_ROOT / "sample.c"), line=7, line_end=25,
        ret_type="int", params=[Param("value", "int", type_info=type_info)], branches=[branch],
        control_vars=[ControlVar(
            name="value", var="value", source="param", var_type="int",
            branch_ids=["B01"], type_info=type_info,
            provenance=_provenance("DeclRefExpr"),
        )],
        provenance=_provenance("FunctionDecl"),
        compile_context={
            "schema_version": 1,
            "language": "c",
            "standard": "c99",
            "source_files": [str(FIXTURE_ROOT / "sample.c")],
            "include_dirs": [str(FIXTURE_ROOT)],
            "defines": {"FEATURE_ON": "1"},
            "force_includes": [str(FIXTURE_ROOT / "config.h")],
            "target_triple": None,
            "cpu": "cortex-m4",
            "abi": None,
            "sysroot": None,
            "resource_dir": None,
            "extra_args": [],
        },
        extensions={"future_fact": {"source": "fixture"}},
    )
    return ir.to_dict()


def test_fixture_fact_manifest_is_complete_and_deterministic():
    manifest = json.loads((FIXTURE_ROOT / "facts.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert (FIXTURE_ROOT / manifest["fixture"]).is_file()
    assert {item["id"] for item in manifest["facts"]} == {
        "config-object-macro", "function-macro-condition", "bitmask-condition",
        "elseif-and-switch-default", "pointer-function-pointer-volatile", "mmio-address-volatile", "invalid-input",
    }
    assert all(item["source"] and item["reason"] for item in manifest["facts"])


def test_valid_document_round_trips_and_function_ir_to_dict_is_top_level_v3():
    document = _document()
    assert document["schema_version"] == 3
    validate_document(document)
    restored = document_to_function_ir(document)
    assert restored.name == "target"
    assert restored.branches[0].atoms[0].cond_text_spelling == "value == MODE_ON"
    assert restored.extensions == {"future_fact": {"source": "fixture"}}
    assert restored.to_dict() == document


def test_serialization_is_byte_stable_and_ends_with_lf():
    document = _document()
    first = serialize_document(document)
    second = serialize_document(copy.deepcopy(document))
    assert first == second
    assert first.endswith("\n")
    assert "\r" not in first


@pytest.mark.parametrize("mutate", [
    lambda value: value.pop("compile_context"),
    lambda value: value.update(schema_version=1),
    lambda value: value["status"].__class__ and value.update(status="MAYBE"),
])
def test_invalid_required_version_and_status_are_rejected(mutate):
    document = _document()
    mutate(document)
    with pytest.raises(FunctionIRSchemaError):
        validate_document(document)


def test_invalid_provenance_range_is_rejected():
    document = _document()
    document["functions"][0]["provenance"]["spelling"]["end_offset"] = 1
    document["functions"][0]["provenance"]["spelling"]["offset"] = 2
    with pytest.raises(FunctionIRSchemaError, match="end_offset"):
        validate_document(document)


def test_duplicate_branch_and_invalid_branch_reference_are_rejected():
    duplicate = _document()
    duplicate["functions"][0]["branches"].append(
        copy.deepcopy(duplicate["functions"][0]["branches"][0])
    )
    with pytest.raises(FunctionIRSchemaError, match="bid 重复"):
        validate_document(duplicate)

    invalid_reference = _document()
    invalid_reference["functions"][0]["control_vars"][0]["branch_ids"] = ["B999"]
    with pytest.raises(FunctionIRSchemaError, match="不存在 branch"):
        validate_document(invalid_reference)


def test_ir_without_extractor_provenance_is_rejected():
    ir = FunctionIR(name="legacy", file="legacy.c", line=1, ret_type="void", line_end=1)
    with pytest.raises(FunctionIRSchemaError, match="缺少 extractor provenance"):
        ir.to_dict()


def test_untyped_semantic_fact_is_rejected():
    document = _document()
    document["functions"][0]["params"][0]["type_info"] = None
    with pytest.raises(FunctionIRSchemaError, match="type_info"):
        validate_document(document)


def test_invalid_c_fixture_fails_without_success_document():
    context = make_compile_context([FIXTURE_ROOT / "invalid.c"], [FIXTURE_ROOT])
    with pytest.raises(ClangExtractorError, match="extractor"):
        ClangExtractor(default_clang_extractor()).extract(
            context, "missing", cwd=FIXTURE_ROOT
        )
