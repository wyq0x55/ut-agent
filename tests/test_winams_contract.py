"""WinAMS 原生 stub / TestCsv 契约测试。

这些测试不依赖外部 ECU 源码，确保主生成入口不会回到项目早期的
``callcnt00`` / ``case_id`` 自定义格式。
"""
from pathlib import Path

from ut_agent.ir import (
    Atom, Branch, CallSite, Case, ControlVar, FunctionIR, MemoryVar, Param,
    Provenance, SourceLocation,
)
from ut_agent.parser import ClangExtractor, default_clang_extractor, make_compile_context
from ut_agent.stub.generate import render_spec_stub_c, render_stub_c
from ut_agent.winams.define_var import (
    DefineVarEntry,
    entries_from_ir,
    render_define_var,
)
from ut_agent.winams.project import (
    GeneratedUnit,
    _amsy_text,
    _winams_batch_args,
    _normalize_reference_mpu,
    _winams_mpu_for_cpu,
    _wsl_to_windows,
)
from ut_agent.rules.model import GenerationResult, NEEDS_REVIEW
from ut_agent.winams.csv_render import build_columns, render_csv, render_intents_csv


def _ir() -> FunctionIR:
    return FunctionIR(
        name="target",
        file="Dma.c",
        line=10,
        ret_type="uint8",
        params=[Param("value", "uint8"),
                Param("out", "uint8 *", is_ptr=True, is_written=True)],
        calls=[CallSite(
            order=0,
            callee="dep",
            line=12,
            params=[Param("arg", "uint16"),
                    Param("result", "uint8 *", is_ptr=True, is_written=True)],
            ret_type="uint8",
        )],
        branches=[Branch(
            bid="B01",
            kind="if",
            line=13,
            cond_text="value == 1",
            atoms=[Atom("value", "uint8", "==", 1, None, "value == 1")],
        )],
        control_vars=[ControlVar("value", "value", "param", var_type="uint8")],
    )


def test_stub_is_winams_native():
    text = render_stub_c(_ir(), call_max=5)
    assert "#define WINAMS_STUB" in text
    assert "AMSTB_dep" in text
    assert "CALLCNT_dep" in text
    assert "ARG00_dep[ CALL_MAX ]" in text
    assert "PTROUT01_dep[ CALL_MAX ]" in text
    assert "AMIN_return[CALL_MAX]" in text
    assert "callcnt00" not in text
    assert "CALLRET00" not in text


def test_stub_does_not_write_back_input_only_pointer():
    ir = FunctionIR(
        name="target",
        file="Dma.c",
        line=1,
        ret_type="void",
        calls=[CallSite(
            order=0,
            callee="inspect",
            line=2,
            params=[Param(
                "data", "uint8 *", is_ptr=True,
                extensions={"write_status": "known"},
            )],
        )],
    )

    text = render_stub_c(ir)

    assert "PTROUT00_inspect" in text
    assert "PTROUT00_inspect[CALLCNT_inspect - 1] = data" in text
    assert "*data = PTROUT00_inspect" not in text

    legacy_text = render_spec_stub_c(ir)
    assert "PTIN00_data" in legacy_text
    assert "PTOUT00_data" not in legacy_text
    assert "*data = PTOUT00_data" not in legacy_text


def test_csv_is_winams_testcsv():
    text = render_csv(_ir(), source_label="Dma.c/target")
    first, declaration, comment, branch = text.splitlines()[:4]
    assert first.startswith('mod,"Dma.c/target","target')
    assert first.split(",")[3:5] == ["6", "5"]
    assert declaration == '%,"AMSTB_dep","dep"'
    assert comment.startswith("#COMMENT,")
    assert '"AMSTB_SrcFile.c/AMSTB_dep@CALLCNT_dep"' in comment
    assert comment.count('@CALLCNT_dep"') == 2
    assert '"AMSTB_SrcFile.c/AMSTB_dep@ARG00_dep[0]"' in comment
    assert '"AMSTB_SrcFile.c/AMSTB_dep@PTROUT01_dep[0]"' in comment
    assert '"CALLCNT_dep"' not in comment
    assert branch == ";$L$,if ( value == 1 )"
    assert ";$L$,TRUE" in text
    assert ";$L$,FALSE" in text
    assert "case_id," not in text
    assert "\r\n" in text
    assert text.encode("cp932")


def test_csv_qualifies_static_stub_declaration_with_source_file():
    ir = _ir()
    ir.calls[0].is_static = True

    rows = list(__import__("csv").reader(__import__("io").StringIO(render_csv(ir))))

    assert rows[1] == ["%", "AMSTB_dep", "Dma.c/dep"]


def test_csv_stub_declarations_use_static_then_external_name_order():
    ir = FunctionIR(
        name="target",
        file="p_blm.c",
        line=1,
        ret_type="void",
        calls=[
            CallSite(order=0, callee="p_vog_orc_pi_req_blmout", line=2),
            CallSite(order=1, callee="p_u1l_blm_jdg_1phase_blmout", line=3,
                     is_static=True),
            CallSite(order=2, callee="p_vog_orc_pi_req_blm_fd_out", line=4),
            CallSite(order=3, callee="p_vog_orc_pi_req_blmpwm", line=5),
            CallSite(order=4, callee="p_u1g_blm_pi_get_fd_out", line=6),
        ],
    )

    rows = list(__import__("csv").reader(__import__("io").StringIO(render_csv(ir))))

    assert [row[1:] for row in rows[1:6]] == [
        ["AMSTB_p_u1l_blm_jdg_1phase_blmout", "p_blm.c/p_u1l_blm_jdg_1phase_blmout"],
        ["AMSTB_p_u1g_blm_pi_get_fd_out", "p_u1g_blm_pi_get_fd_out"],
        ["AMSTB_p_vog_orc_pi_req_blm_fd_out", "p_vog_orc_pi_req_blm_fd_out"],
        ["AMSTB_p_vog_orc_pi_req_blmout", "p_vog_orc_pi_req_blmout"],
        ["AMSTB_p_vog_orc_pi_req_blmpwm", "p_vog_orc_pi_req_blmpwm"],
    ]


def test_csv_qualifies_only_static_test_function_return():
    ir = FunctionIR(name="target", file="Dma.c", line=1, ret_type="uint8")
    comment = next(
        row for row in __import__("csv").reader(
            __import__("io").StringIO(render_csv(ir))
        )
        if row and row[0] == "#COMMENT"
    )
    assert comment[-1] == "target@@"

    ir.extensions["is_static_function"] = True
    static_comment = next(
        row for row in __import__("csv").reader(
            __import__("io").StringIO(render_csv(ir))
        )
        if row and row[0] == "#COMMENT"
    )
    assert static_comment[-1] == "Dma.c/target@@"


def test_render_intents_includes_stub_declarations_and_columns():
    ir = _ir()
    result = GenerationResult(ir.name, NEEDS_REVIEW, issues=("validation-only",))

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_intents_csv(ir, result))
    ))
    declarations = [row for row in rows if row and row[0] == "%"]
    comment = next(row for row in rows if row and row[0] == "#COMMENT")

    assert declarations == [["%", "AMSTB_dep", "dep"]]
    assert "AMSTB_SrcFile.c/AMSTB_dep@CALLCNT_dep" in comment
    assert "AMSTB_SrcFile.c/AMSTB_dep@ARG00_dep[0]" in comment
    assert "AMSTB_SrcFile.c/AMSTB_dep@PTROUT01_dep[0]" in comment


def test_csv_emits_switch_case_and_default_rows():
    ir = FunctionIR(
        name="switch_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        branches=[Branch(
            bid="B00",
            kind="switch",
            line=2,
            cond_text="switch (state)",
            cases=[
                Case("case 0", 0, False),
                Case("case 2", 2, False),
                Case("default", None, True),
            ],
        )],
        control_vars=[ControlVar("state", "state", "global", var_type="uint8")],
    )

    text = render_csv(ir)

    assert ";$L$,switch (state)" in text
    assert text.count(";$L$,case 0:") == 1
    assert text.count(";$L$,case 2:") == 1
    assert text.count(";$L$,default:") == 1


def test_csv_keeps_non_control_global_references_as_inputs():
    ir = FunctionIR(
        name="global_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        globals_used=["g_status", "g_buffer"],
        control_vars=[ControlVar("state", "state", "global", var_type="uint8")],
    )

    text = render_csv(ir)

    assert '#COMMENT,"g_status","g_buffer","state"' in text


def test_csv_does_not_emit_output_for_input_only_pointer():
    ir = FunctionIR(
        name="input_only_pointer",
        file="Dma.c",
        line=1,
        ret_type="void",
        params=[Param("data", "uint8 *", is_ptr=True, is_written=False)],
    )

    text = render_csv(ir)
    comment = next(line for line in text.splitlines()
                   if line.startswith("#COMMENT"))

    assert '"@data"' in comment
    assert '"*data"' not in comment
    assert '"data_out"' not in comment
    assert text.splitlines()[0].split(",")[3:5] == ["1", "0"]

    legacy_headers = [column["header"] for column in build_columns(ir, {})]
    assert "data(设定)" in legacy_headers
    assert "data_out(期待)" not in legacy_headers


def test_csv_does_not_emit_rte_read_receive_pointer_as_output():
    ir = FunctionIR(
        name="target",
        file="Dma.c",
        line=1,
        ret_type="void",
        calls=[CallSite(
            order=0,
            callee="Rte_Read_status",
            line=2,
            params=[Param("data", "uint8 *", is_ptr=True)],
        )],
    )
    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    assert comment == [
        "AMSTB_SrcFile.c/AMSTB_Rte_Read_status@CALLCNT_Rte_Read_status",
        "AMSTB_SrcFile.c/AMSTB_Rte_Read_status@PTROUT00_Rte_Read_status[0]",
        "AMSTB_SrcFile.c/AMSTB_Rte_Read_status@CALLCNT_Rte_Read_status",
    ]


def test_csv_keeps_stub_return_before_pointer_input():
    ir = FunctionIR(
        name="target",
        file="Dma.c",
        line=1,
        ret_type="void",
        calls=[CallSite(
            order=0,
            callee="pal_u1g_mem_get_cdt_scn1",
            line=2,
            ret_type="uint8",
            params=[Param("data", "uint8 *", is_ptr=True)],
        )],
    )
    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    assert comment[:3] == [
        "AMSTB_SrcFile.c/AMSTB_pal_u1g_mem_get_cdt_scn1@CALLCNT_pal_u1g_mem_get_cdt_scn1",
        "AMSTB_SrcFile.c/AMSTB_pal_u1g_mem_get_cdt_scn1@AMIN_return[0]",
        "AMSTB_SrcFile.c/AMSTB_pal_u1g_mem_get_cdt_scn1@PTROUT00_pal_u1g_mem_get_cdt_scn1[0]",
    ]
    assert all("@PTROUT00_pal_u1g_mem_get_cdt_scn1" not in item
               for item in comment[3:])


def test_csv_expands_pal_setter_address_as_nested_data_element():
    ir = FunctionIR(
        name="target",
        file="Dma.c",
        line=1,
        ret_type="void",
        calls=[CallSite(
            order=0,
            callee="pal_u1g_mem_set_cdt_scn1",
            line=2,
            params=[Param("data", "uint8 *", is_ptr=True)],
            extensions={"pointer_arguments": {
                "0": {"is_address": True, "is_null": False},
            }},
        )],
    )
    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    prefix = "AMSTB_SrcFile.c/AMSTB_pal_u1g_mem_set_cdt_scn1@"
    assert comment == [
        prefix + "CALLCNT_pal_u1g_mem_set_cdt_scn1",
        prefix + "PTROUT00_pal_u1g_mem_set_cdt_scn1[0]",
        prefix + "PTROUT00_pal_u1g_mem_set_cdt_scn1[0][0]",
        prefix + "CALLCNT_pal_u1g_mem_set_cdt_scn1",
        prefix + "PTROUT00_pal_u1g_mem_set_cdt_scn1[0][0]",
    ]


def test_csv_selects_fields_under_observed_aggregate_access():
    ir = FunctionIR(
        name="union_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        extensions={"global_objects": [{
            "name": "g_union",
            "read": False,
            "write": True,
            "field_paths": ["active.low", "active.high", "other.value"],
            "extensions": {"field_accesses": [{
                "path": "active", "read": False, "write": True,
            }]},
        }]},
    )

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    assert comment[:2] == [
        "Dma.c/g_union.active.low",
        "Dma.c/g_union.active.high",
    ]
    assert "Dma.c/g_union.other.value" not in comment
    assert comment[-2:] == [
        "Dma.c/g_union.active.low",
        "Dma.c/g_union.active.high",
    ]


def test_csv_does_not_expand_read_aliases_of_union_alternatives():
    ir = FunctionIR(
        name="union_read_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        extensions={"global_objects": [{
            "name": "g_union",
            "read": True,
            "write": False,
            "is_union": True,
            "field_paths": ["active.low", "active.high", "other.value"],
            "extensions": {"field_accesses": [
                {"path": "active.low", "read": True, "write": False},
                {"path": "other.value", "read": True, "write": False},
            ]},
        }]},
    )

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    assert comment == [
        "Dma.c/g_union.active.low",
        "Dma.c/g_union.other.value",
    ]


def test_csv_expands_only_written_union_member_aggregate():
    ir = FunctionIR(
        name="union_mixed_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        extensions={"global_objects": [{
            "name": "g_union",
            "read": True,
            "write": True,
            "is_union": True,
            "field_paths": ["active.low", "active.high", "other.value"],
            "extensions": {"field_accesses": [
                {"path": "active", "read": False, "write": True},
                {"path": "other", "read": True, "write": False},
                {"path": "other.value", "read": True, "write": False},
            ]},
        }]},
    )

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    assert comment == [
        "Dma.c/g_union.active.low",
        "Dma.c/g_union.active.high",
        "Dma.c/g_union.other.value",
        "Dma.c/g_union.active.low",
        "Dma.c/g_union.active.high",
        "Dma.c/g_union.other.value",
    ]


def test_clang_tracks_known_call_pointer_write_direction(tmp_path):
    source = tmp_path / "Dma.c"
    source.write_text(
        "typedef unsigned char uint8;\n"
        "void inspect(uint8 *data) { uint8 sink = *data; (void)sink; }\n"
        "void write_back(uint8 *data) { *data = 1; }\n"
        "void target(uint8 *data) { inspect(data); write_back(data); }\n",
        encoding="utf-8",
    )

    context = make_compile_context([source])
    ir = ClangExtractor(default_clang_extractor()).extract(
        context, "target", cwd=tmp_path
    )

    directions = {
        call.callee: call.params[0]
        for call in ir.calls
        if call.params
    }
    assert directions["inspect"].extensions["write_status"] == "known"
    assert not directions["inspect"].is_written
    assert directions["write_back"].extensions["write_status"] == "known"
    assert directions["write_back"].is_written


def test_csv_expands_clang_global_object_array_evidence():
    ir = FunctionIR(
        name="array_target",
        file="p_mem.c",
        line=1,
        ret_type="void",
        globals_used=["g_buffer"],
        global_writes=["g_buffer"],
        extensions={"global_objects": [{
            "name": "g_buffer",
            "read": True,
            "write": True,
            "array_sizes": [2],
        }]},
    )

    text = render_csv(ir)

    assert text.splitlines()[0].split(",")[3:5] == ["2", "2"]
    assert '#COMMENT,"p_mem.c/g_buffer[0]","p_mem.c/g_buffer[1]"' in text


def test_csv_does_not_repeat_full_cartesian_rows_for_each_branch():
    ir = FunctionIR(
        name="two_branch_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        branches=[
            Branch(
                bid="B00", kind="if", line=2, cond_text="mode == 1",
                atoms=[Atom("mode", "uint8", "==", 1, None, "mode == 1")],
            ),
            Branch(
                bid="B01", kind="if", line=3, cond_text="state == 1",
                atoms=[Atom("state", "uint8", "==", 1, None, "state == 1")],
            ),
        ],
        control_vars=[
            ControlVar("mode", "mode", "global", var_type="uint8"),
            ControlVar("state", "state", "global", var_type="uint8"),
        ],
    )

    text = render_csv(ir)

    # Each branch owns its four relevant boundary values (true/false
    # partitions); the 4x4 global product must not be emitted twice.
    assert text.count("\r\n,") == 8


def test_stub_and_csv_keep_first_call_order_with_unique_callcnt():
    ir = FunctionIR(
        name="ordered_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        calls=[
            CallSite(order=2, callee="second", line=4),
            CallSite(order=0, callee="first", line=2),
            CallSite(order=1, callee="second", line=3),
        ],
    )

    csv_text = render_csv(ir)
    comment = next(line for line in csv_text.splitlines()
                   if line.startswith("#COMMENT"))
    first_positions = [
        index for index in range(len(comment))
            if comment.startswith('"AMSTB_SrcFile.c/AMSTB_first@CALLCNT_first"', index)
    ]
    second_positions = [
        index for index in range(len(comment))
            if comment.startswith('"AMSTB_SrcFile.c/AMSTB_second@CALLCNT_second"', index)
    ]
    assert len(first_positions) == len(second_positions) == 2
    assert first_positions[0] < second_positions[0]
    assert first_positions[1] < second_positions[1]

    stub_text = render_stub_c(ir)
    assert stub_text.index("AMSTB_first") < stub_text.index("AMSTB_second")
    assert stub_text.count("WINAMS_STUB[Dma.c:second") == 1


def test_csv_columns_have_contract_order_and_exclude_function_locals():
    ir = FunctionIR(
        name="ordered_target",
        file="Dma.c",
        line=1,
        ret_type="uint8",
        params=[Param("arg", "uint8"),
                Param("out", "uint8 *", is_ptr=True, is_written=True)],
        globals_used=["state", "tmp"],
        locals=["tmp", "derived"],
        calls=[
            CallSite(
                order=1, callee="second", line=5,
                params=[Param("value", "uint8")],
            ),
            CallSite(
                order=0, callee="first", line=3,
                ret_type="uint8",
            ),
        ],
        branches=[Branch(
            bid="B00", kind="if", line=2, cond_text="state == 1",
            atoms=[Atom("state", "uint8", "==", 1, None, "state == 1")],
        )],
        control_vars=[
            ControlVar("state", "state", "global", var_type="uint8"),
            ControlVar("tmp", "tmp", "local", var_type="uint8"),
            ControlVar("derived", "derived", "local_from_global", var_type="uint8"),
        ],
        extensions={"global_objects": [{
            "name": "state", "read": True, "write": True,
        }]},
    )

    rows = list(__import__("csv").reader(__import__("io").StringIO(render_csv(ir))))
    comments = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    input_count = int(rows[0][3])
    output_count = int(rows[0][4])
    inputs = comments[:input_count]
    outputs = comments[input_count:input_count + output_count]

    assert inputs[:2] == [
        "AMSTB_SrcFile.c/AMSTB_first@CALLCNT_first",
        "AMSTB_SrcFile.c/AMSTB_second@CALLCNT_second",
    ]
    assert inputs[2:4] == ["arg", "@out"]
    assert inputs.index("Dma.c/state") < inputs.index(
        "AMSTB_SrcFile.c/AMSTB_first@AMIN_return[0]"
    )
    assert outputs[:2] == [
        "AMSTB_SrcFile.c/AMSTB_first@CALLCNT_first",
        "AMSTB_SrcFile.c/AMSTB_second@CALLCNT_second",
    ]
    assert outputs[2:4] == ["*out",
                             "AMSTB_SrcFile.c/AMSTB_second@ARG00_second[0]"]
    assert outputs[-1] == "ordered_target@@"
    assert "tmp" not in comments
    assert "derived" not in comments


def test_csv_groups_pre_call_globals_before_repeated_stub_fields():
    ir = FunctionIR(
        name="grouped_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        calls=[
            CallSite(order=0, callee="first", line=10, ret_type="uint8"),
            CallSite(
                order=1, callee="helper", line=20, ret_type="uint8",
                params=[Param("value", "uint8"), Param("timer", "uint8")],
            ),
            CallSite(
                order=2, callee="helper", line=30, ret_type="uint8",
                params=[Param("value", "uint8"), Param("timer", "uint8")],
            ),
            CallSite(
                order=3, callee="helper", line=40, ret_type="uint8",
                params=[Param("value", "uint8"), Param("timer", "uint8")],
            ),
        ],
        extensions={"global_objects": [
            {"name": "g_first", "read": True, "write": False,
             "extensions": {"read_line": 20}},
            {"name": "g_second", "read": True, "write": False,
             "extensions": {"read_line": 30}},
            {"name": "g_third", "read": True, "write": False,
             "extensions": {"read_line": 40}},
            {"name": "g_after", "read": True, "write": False,
             "extensions": {"read_line": 50}},
        ]},
    )

    rows = list(__import__("csv").reader(__import__("io").StringIO(render_csv(ir))))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    first_stub = "AMSTB_SrcFile.c/AMSTB_helper@ARG00_helper[0]"
    assert comment.index("Dma.c/g_first") < comment.index(first_stub)
    assert comment.index("Dma.c/g_third") < comment.index(first_stub)
    assert comment.index(first_stub) < comment.index("Dma.c/g_after")


def test_csv_orders_struct_fields_by_field_access_position():
    ir = FunctionIR(
        name="target",
        file="p_mem.c",
        line=1,
        ret_type="void",
        calls=[CallSite(
            order=0,
            callee="helper",
            line=20,
            params=[Param("value", "uint8")],
        )],
        extensions={"global_objects": [{
            "name": "g_state",
            "read": True,
            "write": True,
            "field_paths": ["late", "early"],
            "extensions": {"field_accesses": [
                {"path": "early", "read": True, "write": True,
                 "read_line": 10, "write_line": 10},
                {"path": "late", "read": True, "write": True,
                 "read_line": 30, "write_line": 30},
            ]},
        }]},
    )

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    early = "p_mem.c/g_state.early"
    late = "p_mem.c/g_state.late"
    stub = "AMSTB_SrcFile.c/AMSTB_helper@ARG00_helper[0]"
    assert comment.index(early) < comment.index(stub) < comment.index(late)


def test_csv_places_global_write_at_same_source_event_before_stub_input():
    ir = FunctionIR(
        name="target",
        file="p_mem.c",
        line=1,
        ret_type="void",
        calls=[CallSite(
            order=0,
            callee="helper",
            line=20,
            params=[Param("value", "uint8")],
        )],
        extensions={"global_objects": [{
            "name": "g_count",
            "read": True,
            "write": True,
            "extensions": {"read_line": 20, "write_line": 20},
        }]},
    )

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row[1:] for row in rows if row and row[0] == "#COMMENT")
    global_column = "p_mem.c/g_count"
    stub = "AMSTB_SrcFile.c/AMSTB_helper@ARG00_helper[0]"
    assert comment.index(global_column) < comment.index(stub)


def test_render_intents_preserves_parsed_switch_cases_without_oracle():
    ir = FunctionIR(
        name="switch_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        branches=[Branch(
            bid="B00", kind="switch", line=2, cond_text="switch (state)",
            cases=[Case("case 0", 0, False), Case("case 2", 2, False),
                   Case("default", None, True)],
        )],
        control_vars=[ControlVar("state", "state", "global")],
    )
    result = GenerationResult("switch_target", NEEDS_REVIEW, issues=("oracle",))

    text = render_intents_csv(ir, result)

    assert ";$L$,switch (state)" in text
    assert text.count(";$L$,case 0:") == 1
    assert text.count(";$L$,case 2:") == 1
    assert text.count(";$L$,default:") == 1


def test_render_intents_nests_if_under_own_switch_case():
    def provenance(start, end, line, kind):
        location = SourceLocation("p_blm.c", line, 1, start, end)
        return Provenance(location, location, ast_kind=kind)

    ir = FunctionIR(
        name="p_vol_blm_job_out",
        file="p_blm.c",
        line=821,
        ret_type="void",
        branches=[
            Branch(
                bid="b0", kind="switch", line=821,
                cond_text="switch ( state )",
                cases=[
                    Case("0", 0, False, provenance(10, 10, 823, "CaseStmt")),
                    Case("1", 1, False, provenance(20, 80, 826, "CaseStmt")),
                    Case("2", 2, False, provenance(81, 140, 836, "CaseStmt")),
                    Case("default", None, True, provenance(141, 150, 856, "DefaultStmt")),
                ],
                provenance=provenance(1, 150, 821, "SwitchStmt"),
            ),
            Branch(
                bid="b1", kind="if", line=827,
                cond_text="state == 1",
                provenance=provenance(30, 80, 827, "IfStmt"),
            ),
            Branch(
                bid="b2", kind="if", line=837,
                cond_text="state == 2",
                provenance=provenance(90, 140, 837, "IfStmt"),
            ),
        ],
        control_vars=[ControlVar("state", "state", "global")],
    )
    result = GenerationResult("p_vol_blm_job_out", NEEDS_REVIEW, issues=("oracle",))

    lines = [line for line in render_intents_csv(ir, result).splitlines()
             if line.startswith(";$L$")]
    assert lines.index(";$L$,case 1:") < lines.index(";$L$,if ( state == 1 )")
    assert lines.index(";$L$,if ( state == 1 )") < lines.index(";$L$,case 2:")
    assert lines.index(";$L$,case 2:") < lines.index(";$L$,if ( state == 2 )")

    legacy_lines = [line for line in render_csv(ir).splitlines()
                    if line.startswith(";$L$")]
    assert legacy_lines.index(";$L$,case 1:") < legacy_lines.index(";$L$,if ( state == 1 )")
    assert legacy_lines.index(";$L$,if ( state == 1 )") < legacy_lines.index(";$L$,case 2:")
    assert legacy_lines.index(";$L$,case 2:") < legacy_lines.index(";$L$,if ( state == 2 )")


def test_arm_gcc_cpu_maps_to_installed_winams_mpu_names():
    assert _winams_mpu_for_cpu("cortex-m3") == "CortexM3(GCC)"
    assert _winams_mpu_for_cpu("cortex-m4") == "Cortex-M4(ARM GCC soft)"
    assert _winams_mpu_for_cpu("rh850") == "RH850"


def test_reference_amsy_uses_rh850_ghs_and_external_object():
    unit_dir = Path("C:/reference-rh850/target")
    reference_out = Path("C:/reference/Soft.out")
    reference_xlo = Path("C:/reference/Soft.out.xlo")
    text = _amsy_text(
        unit_dir,
        reference_xlo,
        reference_out,
        Path("C:/reference/AMSTB_SrcFile.c"),
    )
    mpu = _normalize_reference_mpu("RH850(GHS)")
    text = text.replace(
        "MpuFixedName=Cortex-M4(ARM GCC soft)",
        f"MpuFixedName={mpu}",
    ).replace(
        "MpuModelFixedName=Cortex-M4(ARM GCC soft)",
        f"MpuModelFixedName={mpu}",
    ).replace(
        "ToolFixedName=ARM GCC OMF Converter",
        "ToolFixedName=GHS",
    )
    assert "ObjectFile=" in text
    assert "InFile=" in text
    assert "MpuFixedName=RH850" in text
    assert "MpuModelFixedName=RH850" in text
    assert "ToolFixedName=GHS" in text
    assert "Cortex-M4(ARM GCC soft)" not in text
    assert _normalize_reference_mpu("RH850") == "RH850"


def test_winams_batch_arguments_put_project_last_and_pass_csv_output():
    unit = GeneratedUnit(
        name="target",
        source=Path("C:/reference/src/target.c"),
        stub=Path("C:/reference/src/AMSTB_SrcFile.c"),
        testcsv=Path("C:/reference/target/TestCsv/target.csv"),
        expected=None,
        elf=Path("C:/reference/Soft.out"),
        xlo=Path("C:/reference/Soft.out.xlo"),
        amsy=Path("C:/reference/target/target.amsy"),
        output=Path("C:/reference/target/Output"),
    )
    args = _winams_batch_args(unit)
    assert args == [
        "-b",
        "-output", str(unit.output),
        "-testCsv", "target.csv",
        "-xmlex", str(unit.output / "target.amsyr"),
        str(unit.amsy),
    ]


def test_winams_path_conversion_keeps_git_bash_drive_forms():
    assert _wsl_to_windows("/mnt/c/WinAMS/BIN/SSTManager.exe") == (
        "C:\\WinAMS\\BIN\\SSTManager.exe"
    )
    assert _wsl_to_windows("/c/WinAMS/BIN/SSTManager.exe") == (
        "C:\\WinAMS\\BIN\\SSTManager.exe"
    )


def test_define_var_uses_winams_io_registration_format():
    ir = FunctionIR(
        name="target", file="Dma.c", line=1, ret_type="void",
        memory_vars=[MemoryVar(
            name="U2L_DMA_REG_ICDMA04", address=0xFFFFB080, width=2,
        )],
        control_vars=[ControlVar(
            name="state", var="Dcm_PbRamNetComCtrlChannels.state",
            source="global",
        )],
        global_writes=["Dcm_PbRamNetComCtrlChannels"],
    )
    entries = entries_from_ir(ir)
    assert [entry.name for entry in entries] == ["U2L_DMA_REG_ICDMA04"]
    assert all(entry.definition for entry in entries)
    assert render_define_var([*entries, DefineVarEntry(name="unused_global")]) == (
        '8,"U2L_DMA_REG_ICDMA04","0xFFFFB080#U2#2"\r\n'
    )


def test_clang_extracts_memory_mapped_address_and_access_width(tmp_path):
    source = tmp_path / "Dma.c"
    source.write_text(
        """
typedef unsigned long u4;
typedef unsigned short u2;
#define U4L_DMA_REG_ICDMA04 ((u4)0xFFFFB080UL)
void p_vol_dma_orwrite_reg16(u4 address, u2 value) { (void)address; (void)value; }
void target(void) { p_vol_dma_orwrite_reg16(U4L_DMA_REG_ICDMA04, 1U); }
""",
        encoding="utf-8",
    )
    context = make_compile_context([source])
    ir = ClangExtractor(default_clang_extractor()).extract(
        context, "target", cwd=tmp_path
    )

    assert len(ir.memory_vars) == 1
    memory = ir.memory_vars[0]
    assert memory.name == "U2L_DMA_REG_ICDMA04"
    assert memory.address == 0xFFFFB080
    assert memory.width == 2
    assert memory.write and not memory.read
    assert memory.input_value == 0
    assert memory.expected_value == 1

    entry = entries_from_ir(ir)[0]
    assert entry.name == "U2L_DMA_REG_ICDMA04"
    assert entry.definition == "0xFFFFB080#U2#2"
    csv_text = render_csv(ir)
    assert '#COMMENT,"U2L_DMA_REG_ICDMA04","U2L_DMA_REG_ICDMA04"' in csv_text
    assert ",0x0,0x1" in csv_text
    assert "AMSTB_p_vol_dma_orwrite_reg16" not in csv_text


def test_clang_drops_write_only_io_when_high_level_output_exists(tmp_path):
    source = tmp_path / "Dma.c"
    source.write_text(
        ""
        "typedef unsigned long u4;\n"
        "typedef unsigned short u2;\n"
        "typedef unsigned char u1;\n"
        "#define U4L_DMA_REG_ICDMA04 ((u4)0xFFFFB080UL)\n"
        "void p_vol_dma_write_reg32(u4 address, u4 value) { (void)address; (void)value; }\n"
        "u1 target(void) {\n"
        "  p_vol_dma_write_reg32(U4L_DMA_REG_ICDMA04, 1U);\n"
        "  return 1U;\n"
        "}\n",
        encoding="utf-8",
    )
    context = make_compile_context([source])
    ir = ClangExtractor(default_clang_extractor()).extract(
        context, "target", cwd=tmp_path
    )

    assert ir.memory_vars == []
    assert "U4L_DMA_REG_ICDMA04" not in render_csv(ir)
    assert "AMSTB_p_vol_dma_write_reg32" not in render_csv(ir)


def test_clang_propagates_const_array_member_and_marks_dead_branch(tmp_path):
    header = tmp_path / "cfg.h"
    header.write_text(
        """
typedef struct { int unit_id; int is_enable_dma; } Cfg;
enum { SPI_CSIH2 = 0 };
extern const Cfg xnl_spi_unit_cfg[1];
""",
        encoding="utf-8",
    )
    source = tmp_path / "Dma.c"
    source.write_text(
        '#include "cfg.h"\n'
        "void target(void) {\n"
        "  if (1 == xnl_spi_unit_cfg[SPI_CSIH2].is_enable_dma) { }\n"
        "}\n",
        encoding="utf-8",
    )
    config = tmp_path / "Cfg.c"
    config.write_text(
        '#include "cfg.h"\n'
        "const Cfg xnl_spi_unit_cfg[1] = { { 7, 1 } };\n",
        encoding="utf-8",
    )
    context = make_compile_context([source, config], [tmp_path])
    ir = ClangExtractor(default_clang_extractor()).extract_from_source(
        context, "target", source, cwd=tmp_path
    )

    assert len(ir.branches) == 1
    assert ir.branches[0].constant_value is True
    assert "AST values: 1 == 1" in (ir.branches[0].constant_reason or "")
    control = next(cv for cv in ir.control_vars if "is_enable_dma" in cv.var)
    assert control.constant_value == 1
    csv_text = render_csv(ir)
    assert "FALSE デッドコードがあった為、この分岐に入ることができません" in csv_text
    assert ",FALSE デッドコードがあった為、この分岐に入ることができません" in csv_text
    assert csv_text.count(";$L$,TRUE") == 1
