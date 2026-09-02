"""Architecture gates for the FunctionIR and domain migration in Issue #3."""
from __future__ import annotations

import json
import ast
import re
from pathlib import Path

from ut_agent.generation.boundary import control_candidates
from ut_agent.ir import (
    Atom, Branch, CallSite, Case, ControlVar, FunctionIR, Param, TypeInfo,
    ValueOrigin,
)
from ut_agent.generation.engine import evaluate_atom, evaluate_branch
from ut_agent.targets.winams.stub import render_stub_c

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ut_agent"
EXTRACTOR = ROOT / "tooling" / "ut-clang-extract"


def _python_sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_issue3_domain_layout_and_gate():
    old = [SRC / name for name in
           ("ir.py", "cli.py", "parser", "rules", "cases", "winams", "host", "stub", "artifacts")]
    assert not any(path.is_file() or any(path.glob("*.py")) for path in old)
    from script.check_architecture import violations
    assert violations() == []


def test_project_manifest_resolves_locked_baseline(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "project": {"id": "N-O2606-PSD-049"},
        "baseline": {"id": "psd-rebuild-mcdc", "version": "1.0"},
        "rules": {"project_pack": None},
        "build": {"profile": "default"},
        "winams": {"profile": "standard"},
    }), encoding="utf-8")
    from ut_agent.project import resolve_project_context
    context = resolve_project_context(manifest, config_root=ROOT / "config")
    assert context.project_id == "N-O2606-PSD-049"
    assert context.baseline_ref == "psd-rebuild-mcdc@1.0"
    assert context.provenance["function_ir_version"] == 3


def test_python_pipeline_has_no_c_parser_or_core_extension_reads():
    text = "\n".join(path.read_text(encoding="utf-8") for path in _python_sources())
    assert "clang.cindex" not in text
    assert "extensions.get" not in text
    assert "infer_branch_nesting" not in text
    assert "is_scalar_type" not in text
    assert "selected_global_writes" not in text
    assert "_BITMASK" not in text
    assert "_MEMORY_HELPER" not in text


def test_generation_does_not_reopen_source_or_own_target_column_spelling():
    engine = (SRC / "generation" / "engine.py").read_text(encoding="utf-8")
    corpus = (SRC / "learning" / "corpus.py").read_text(encoding="utf-8")
    model = (SRC / "generation" / "model.py").read_text(encoding="utf-8")
    semantic = (SRC / "generation" / "semantic.py").read_text(encoding="utf-8")
    projection = (SRC / "targets" / "winams" / "projection.py").read_text(encoding="utf-8")
    renderer = (SRC / "targets" / "winams" / "csv.py").read_text(encoding="utf-8")
    assert "AMSTB_" not in engine + semantic
    assert "PTROUT" not in engine + semantic
    assert "AMIN_return" not in engine + semantic
    assert "CALLCNT" not in engine + semantic
    assert "read_text" not in engine + model + semantic
    assert "rglob" not in engine + model + semantic
    assert "input_columns" not in model
    assert "output_columns" not in model
    assert "stub_declarations" not in model
    assert not (SRC / "generation" / "infer.py").exists()
    assert "Rte_Read_" not in projection + renderer
    assert "pal_" not in projection + renderer


def test_generation_dependency_graph_has_no_learning_or_target_imports():
    """Generation may emit semantic IDs but cannot depend on adapters/learning."""
    generation_root = SRC / "generation"
    forbidden = {"ut_agent.targets.winams", "ut_agent.toolchain", "ut_agent.learning"}
    for path in generation_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module or ""}
            else:
                continue
            assert not any(
                name == prefix or name.startswith(prefix + ".")
                for name in names for prefix in forbidden
            ), f"{path} imports execution adapter: {names}"


def test_winams_target_does_not_import_offline_learning():
    target_root = SRC / "targets" / "winams"
    forbidden = {"ut_agent.learning"}
    for path in target_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module or ""}
            else:
                continue
            assert not any(
                name == prefix or name.startswith(prefix + ".")
                for name in names for prefix in forbidden
            ), f"{path} imports offline learning: {names}"


def test_project_package_does_not_own_target_materialization():
    project_root = SRC / "project"
    forbidden = {"ut_agent.targets.winams"}
    for path in project_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module or ""}
            else:
                continue
            assert not any(
                name == prefix or name.startswith(prefix + ".")
                for name in names for prefix in forbidden
            ), f"{path} imports target materialization: {names}"


def test_learning_owns_corpus_extraction_orchestration():
    collector = (SRC / "learning" / "corpus.py").read_text(encoding="utf-8")
    assert "ClangExtractor" in collector
    assert "make_compile_context" in collector


def test_extractor_pass_boundary_and_v3_schema_are_present():
    schema = json.loads(
        (ROOT / "schemas" / "function-ir-v3.schema.json").read_text(encoding="utf-8")
    )
    assert schema["$id"].endswith("function-ir-v3.json")
    assert schema["properties"]["schema_version"]["const"] == 3
    cmake = (EXTRACTOR / "CMakeLists.txt").read_text(encoding="utf-8")
    main = (EXTRACTOR / "main.cpp").read_text(encoding="utf-8")
    pass_sources = sorted(set(re.findall(r"passes/[A-Za-z0-9_]+\.cpp", cmake)))
    assert pass_sources
    for source in pass_sources:
        source_path = EXTRACTOR / source.replace("/", "\\")
        assert source_path.is_file(), source
        header = source[:-4] + ".h"
        assert (EXTRACTOR / header.replace("/", "\\")).is_file(), header
        assert f'#include "{header}"' in main
    assert "parameterWriteEffects()" in main
    assert '"order", Effect.Order' in main


def test_ir_validation_has_a_public_package_boundary():
    from ut_agent.ir import FunctionIR, Provenance, SourceLocation, validate_ir
    from ut_agent.toolchain import make_compile_context

    location = SourceLocation("target.c", 1, 1, 0, 0)
    validate_ir(FunctionIR(
        "target", "target.c", 1, "void", line_end=1,
        provenance=Provenance(location, location, ast_kind="FunctionDecl"),
        compile_context=make_compile_context([Path("target.c")]).to_dict(),
    ))


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


def test_switch_default_candidate_stays_inside_typed_domain():
    info = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    ir = FunctionIR(
        name="target", file="target.c", line=1, ret_type="void",
        branches=[Branch(
            bid="b0", kind="switch", line=2,
            cases=[Case("case 255:", 255, False), Case("default:", None, True)],
            selector=ValueOrigin(kind="variable", driver="state"),
        )],
        control_vars=[ControlVar(
            "state", "state", "param", var_type="unsigned char",
            type_info=info, branch_ids=["b0"],
        )],
    )
    values = control_candidates(ir)["state"]["values"]
    assert 256 not in values
    assert all(0 <= value <= 255 for value in values)


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
