"""P3 tests for the process boundary around the standalone Clang extractor."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ut_agent.ir import FunctionIR, Provenance, SourceLocation
from ut_agent.parser.clang_extractor import (
    ClangExtractor,
    ClangExtractorError,
    default_clang_extractor,
    make_compile_context,
)
from ut_agent.parser.ir_json import serialize_document
from ut_agent.winams.csv_render import render_csv


def _document(status: str = "PARTIAL") -> dict:
    location = SourceLocation("sample.c", 1, 1, 0, 1)
    document = FunctionIR(
        name="target", file="sample.c", line=1, line_end=1, ret_type="void",
        provenance=Provenance(location, location, ast_kind="FunctionDecl"),
        compile_context={
            "schema_version": 1,
            "language": "c",
            "standard": "c11",
            "source_files": ["sample.c"],
            "include_dirs": [],
            "defines": {},
            "force_includes": [],
            "target_triple": None,
            "cpu": None,
            "abi": None,
            "sysroot": None,
            "resource_dir": None,
            "extra_args": [],
        },
    ).to_dict()
    document["status"] = status
    if status == "ERROR":
        document["diagnostics"] = [{
            "code": "TEST_ERROR",
            "severity": "error",
            "message": "synthetic extractor failure",
        }]
    return document


def test_compile_context_normalizes_and_sorts_defines(tmp_path: Path):
    context = make_compile_context(
        [tmp_path / "sample.c"],
        [tmp_path / "include"],
        {"Z_FLAG": "2", "A_FLAG": "1"},
        [tmp_path / "config.h"],
        cpu="cortex-m4",
    )
    value = context.to_dict()
    assert value["source_files"] == [str((tmp_path / "sample.c").resolve())]
    assert list(value["defines"]) == ["A_FLAG", "Z_FLAG"]
    assert value["cpu"] == "cortex-m4"


def test_client_validates_and_maps_extractor_output(tmp_path: Path, monkeypatch):
    executable = tmp_path / "ut-clang-extract.exe"
    executable.write_bytes(b"stub")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        output = Path(command[command.index("--output") + 1])
        output.write_text(serialize_document(_document()), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "ut_agent.parser.clang_extractor.subprocess.run", fake_run
    )
    client = ClangExtractor(executable)
    context = make_compile_context([tmp_path / "sample.c"])
    ir = client.extract(context, "target", cwd=tmp_path)
    assert ir.name == "target"
    assert seen["command"][-2:] == ["--function", "target"]
    assert seen["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert seen["kwargs"]["check"] is False


def test_client_rejects_error_document(tmp_path: Path, monkeypatch):
    executable = tmp_path / "ut-clang-extract.exe"
    executable.write_bytes(b"stub")

    def fake_run(command, **kwargs):
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            serialize_document(_document("ERROR")), encoding="utf-8"
        )
        return SimpleNamespace(returncode=1, stdout="", stderr="clang failed")

    monkeypatch.setattr(
        "ut_agent.parser.clang_extractor.subprocess.run", fake_run
    )
    with pytest.raises(ClangExtractorError, match="synthetic extractor failure"):
        ClangExtractor(executable).extract(
            make_compile_context([tmp_path / "sample.c"]), "target"
        )


def test_client_rejects_missing_executable(tmp_path: Path):
    with pytest.raises(ClangExtractorError, match="not found"):
        ClangExtractor(tmp_path / "missing.exe").extract(
            make_compile_context([tmp_path / "sample.c"]), "target"
        )


def test_standalone_extracts_expanded_macro_and_member_lvalue(tmp_path: Path):
    executable = default_clang_extractor()
    if executable is None:
        pytest.skip("repository standalone extractor is not built")
    header = tmp_path / "config.h"
    header.write_text(
        "typedef unsigned char u1;\n"
        "typedef struct { u1 mode; } Cfg;\n"
        "extern const Cfg table[2];\n"
        "#define MODE_WRITE ((u1)0U)\n"
        "#define LIMIT ((u1)2U)\n",
        encoding="ascii",
    )
    source = tmp_path / "target.c"
    source.write_text(
        '#include "config.h"\n'
        "void target(void) {\n"
        "  u1 index = 0U;\n"
        "  for (; LIMIT > index; ++index) {\n"
        "    if (MODE_WRITE == table[index].mode) { }\n"
        "  }\n"
        "}\n",
        encoding="ascii",
    )
    config = tmp_path / "config.c"
    config.write_text(
        '#include "config.h"\n'
        "const Cfg table[2] = { { MODE_WRITE }, { 1U } };\n",
        encoding="ascii",
    )

    client = ClangExtractor(executable)
    ir = client.extract(
        make_compile_context([source, config], [tmp_path]), "target",
        cwd=tmp_path,
    )

    loop, branch = ir.branches
    assert loop.kind == "for"
    assert loop.cond_text_expanded == "((u1)2U) > index"
    atom = branch.atoms[0]
    assert atom.var.replace(" ", "") == "table[index].mode"
    assert atom.boundary == 0
    assert atom.boundary_name == "MODE_WRITE"
    control = next(item for item in ir.control_vars if item.name == "mode")
    assert control.source == "global"


def test_standalone_extract_all_collects_every_source_function(tmp_path: Path):
    executable = default_clang_extractor()
    if executable is None:
        pytest.skip("repository standalone extractor is not built")
    source = tmp_path / "targets.c"
    source.write_text(
        "void first(void) { }\n"
        "void second(void) { }\n",
        encoding="ascii",
    )

    functions = ClangExtractor(executable).extract_all(
        make_compile_context([source], [tmp_path]), cwd=tmp_path,
    )

    assert [item.name for item in functions] == ["first", "second"]


def test_standalone_resolves_function_pointer_table_initializer(tmp_path: Path):
    executable = default_clang_extractor()
    if executable is None:
        pytest.skip("repository standalone extractor is not built")
    header = tmp_path / "rte.h"
    header.write_text(
        "typedef unsigned char u1;\n"
        "typedef u1 (*Getter)(u1 *data);\n"
        "typedef struct { Getter get; } Entry;\n"
        "extern const Entry table[1];\n"
        "u1 Rte_Read_can_status(u1 *data);\n",
        encoding="ascii",
    )
    source = tmp_path / "target.c"
    source.write_text(
        '#include "rte.h"\n'
        "void target(void) {\n"
        "  u1 value = 0U;\n"
        "  (void)table[0].get(&value);\n"
        "}\n",
        encoding="ascii",
    )
    config = tmp_path / "config.c"
    config.write_text(
        '#include "rte.h"\n'
        "u1 Rte_Read_can_status(u1 *data) { *data = 1U; return 0U; }\n"
        "const Entry table[1] = { { &Rte_Read_can_status } };\n",
        encoding="ascii",
    )

    ir = ClangExtractor(executable).extract(
        make_compile_context([source, config], [tmp_path]), "target",
        cwd=tmp_path,
    )

    call = ir.calls[0]
    assert call.callee == "Rte_Read_can_status"
    assert call.ptr_call is False
    assert call.table_base == "table"
    assert call.table_member == "get"
    assert call.extensions["resolved_via"] == "function_pointer_initializer"
    assert call.params[0].is_ptr is True
    assert call.extensions["caller_param_output"]["0"] is False


def test_standalone_propagates_scalar_global_initializer_from_context_tu(
    tmp_path: Path,
):
    executable = default_clang_extractor()
    if executable is None:
        pytest.skip("repository standalone extractor is not built")
    header = tmp_path / "state.h"
    header.write_text("extern const int state;\n", encoding="ascii")
    source = tmp_path / "target.c"
    source.write_text(
        '#include "state.h"\n'
        "void target(void) { if (state == 1) { } }\n",
        encoding="ascii",
    )
    context_source = tmp_path / "state.c"
    context_source.write_text(
        '#include "state.h"\nconst int state = 1;\n',
        encoding="ascii",
    )

    ir = ClangExtractor(executable).extract(
        make_compile_context([source, context_source], [tmp_path]), "target",
        cwd=tmp_path,
    )

    assert ir.branches[0].constant_value is True
    control = next(item for item in ir.control_vars if item.name == "state")
    assert control.constant_value == 1


def test_standalone_tracks_local_control_value_origin(tmp_path: Path):
    executable = default_clang_extractor()
    if executable is None:
        pytest.skip("repository standalone extractor is not built")
    header = tmp_path / "origin.h"
    header.write_text(
        "typedef unsigned char u1;\n"
        "typedef struct { u1 value; } Entry;\n"
        "extern const Entry table[2];\n"
        "u1 get_status(void);\n",
        encoding="ascii",
    )
    source = tmp_path / "target.c"
    source.write_text(
        '#include "origin.h"\n'
        "void target(u1 index) {\n"
        "  u1 table_value = table[index].value;\n"
        "  u1 status = get_status();\n"
        "  if (table_value == 1U) { }\n"
        "  if (status == 1U) { }\n"
        "}\n",
        encoding="ascii",
    )
    context_source = tmp_path / "origin.c"
    context_source.write_text(
        '#include "origin.h"\n'
        "u1 get_status(void) { return 1U; }\n"
        "const Entry table[2] = { { 0U }, { 1U } };\n",
        encoding="ascii",
    )

    ir = ClangExtractor(executable).extract(
        make_compile_context([source, context_source], [tmp_path]), "target",
        cwd=tmp_path,
    )

    table_control = next(item for item in ir.control_vars if item.name == "table_value")
    assert table_control.source == "derived"
    assert table_control.extensions["value_origin"]["kind"] == "const_table_field"
    assert table_control.extensions["value_origin"]["driver"] == "index"
    assert table_control.extensions["value_origin"]["table_values"] == {
        "0": 0, "1": 1,
    }
    status_control = next(item for item in ir.control_vars if item.name == "status")
    assert status_control.source == "stub"
    assert status_control.extensions["value_origin"]["kind"] == "stub_return"
    assert status_control.extensions["value_origin"]["callee"] == "get_status"


def test_standalone_keeps_function_scope_static_out_of_external_io(tmp_path: Path):
    executable = default_clang_extractor()
    if executable is None:
        pytest.skip("repository standalone extractor is not built")
    source = tmp_path / "target.c"
    source.write_text(
        "static int file_state;\n"
        "void target(void) {\n"
        "  static int internal_state;\n"
        "  if (internal_state == 1) { file_state = 1; }\n"
        "}\n",
        encoding="ascii",
    )

    ir = ClangExtractor(executable).extract(
        make_compile_context([source], [tmp_path]), "target", cwd=tmp_path,
    )

    internal = next(item for item in ir.control_vars if item.name == "internal_state")
    assert internal.source == "local"
    assert "internal_state" not in ir.globals_used
    csv_text = render_csv(ir)
    assert "internal_state" not in csv_text.split("#COMMENT", 1)[1].split("\r\n", 1)[0]


def test_standalone_preserves_switch_case_branch_nesting(tmp_path: Path):
    executable = default_clang_extractor()
    if executable is None:
        pytest.skip("repository standalone extractor is not built")
    source = tmp_path / "target.c"
    source.write_text(
        "void target(int state) {\n"
        "  switch (state) {\n"
        "    case 1:\n"
        "      if (state == 1) { }\n"
        "      break;\n"
        "    case 2:\n"
        "      if (state == 2) { }\n"
        "      break;\n"
        "    default:\n"
        "      break;\n"
        "  }\n"
        "}\n",
        encoding="ascii",
    )

    ir = ClangExtractor(executable).extract(
        make_compile_context([source], [tmp_path]), "target", cwd=tmp_path,
    )

    assert [(branch.kind, branch.parent_bid) for branch in ir.branches] == [
        ("switch", None), ("if", "b0"), ("if", "b0"),
    ]


def test_standalone_tracks_return_and_local_value_effects(tmp_path: Path):
    executable = default_clang_extractor()
    if executable is None:
        pytest.skip("repository standalone extractor is not built")
    source = tmp_path / "return.c"
    source.write_text(
        "typedef unsigned char u1;\n"
        "u1 helper(void);\n"
        "static u1 target(u1 mode) {\n"
        "  u1 result = 1U;\n"
        "  if (mode != 0U) { result = helper(); }\n"
        "  return result;\n"
        "}\n",
        encoding="ascii",
    )
    ir = ClangExtractor(executable).extract(
        make_compile_context([source]), "target", cwd=tmp_path
    )
    effects = ir.extensions["return_effects"]
    assert effects and effects[0]["value"].strip("() ") == "result"
    local_effects = ir.extensions["local_value_effects"]
    assert {item["name"] for item in local_effects} == {"result"}
    assert any(item["constant_value"] == 1 for item in local_effects)
    assert any(item["origin"]["kind"] == "stub_return" for item in local_effects)
