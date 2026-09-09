"""WinAMS 原生 stub / TestCsv 契约测试。

这些测试不依赖外部 ECU 源码，确保主生成入口不会回到项目早期的
``callcnt00`` / ``case_id`` 自定义格式。
"""
from pathlib import Path

from ut_agent.ir import (
    Atom, Branch, CallSite, Case, ControlVar, FunctionIR, GlobalObject,
    MemoryVar, Param,
    Provenance, SourceLocation,
)
from ut_agent.toolchain import ClangExtractor, default_clang_extractor, make_compile_context
from ut_agent.targets.winams.stub import render_spec_stub_c, render_stub_c
from ut_agent.targets.winams.define_var import (
    DefineVarEntry,
    entries_from_ir,
    render_define_var,
)
from ut_agent.targets.winams.project import (
    GeneratedUnit,
    _amsy_text,
    _winams_batch_args,
    _normalize_reference_mpu,
    _winams_mpu_for_cpu,
    _wsl_to_windows,
)
from ut_agent.generation.model import (
    GenerationResult, NEEDS_REVIEW, TestIntent, TestObligation,
    ValidationResult, VALIDATED,
)
from ut_agent.targets.winams.csv import build_columns, render_csv, render_intents_csv


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

    ir.is_static = True
    static_comment = next(
        row for row in __import__("csv").reader(
            __import__("io").StringIO(render_csv(ir))
        )
        if row and row[0] == "#COMMENT"
    )
    assert static_comment[-1] == "Dma.c/target@@"


def test_static_pointer_pointee_column_uses_canonical_semantic_key():
    ir = FunctionIR(
        name="target",
        file="Dma.c",
        line=1,
        ret_type="void",
        params=[Param(
            "data", "uint8 *", is_ptr=True, is_written=True,
            access_paths=[{
                "path": "*data", "read": True, "write": True,
            }],
        )],
    )
    ir.is_static = True
    intent = TestIntent(
        case_id="U001",
        obligation=TestObligation("ENTRY", "execution"),
        inputs={"param:data:address": 1, "param:data:pointee:value": 0},
        expected={"param:data:pointee:value": 1},
        validation=ValidationResult(VALIDATED, checks=("oracle",)),
    )

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_intents_csv(
            ir, GenerationResult("target", VALIDATED, (intent,)),
        ))
    ))
    comment = next(row for row in rows if row and row[0] == "#COMMENT")
    data = next(row for row in rows if row and row[0] == "")

    pointee_positions = [
        index for index, value in enumerate(comment) if value == "@data[0]"
    ]
    assert len(pointee_positions) == 2
    assert data[pointee_positions[-1]] == "0x1"


def test_indexed_pointer_write_is_output_only_and_uses_indexed_semantic_key():
    ir = FunctionIR(
        name="indexed_target",
        file="Dma.c",
        line=1,
        ret_type="void",
        params=[Param(
            "data", "uint8 *", is_ptr=True, is_written=True,
            access_paths=[{
                "path": "data[26]", "read": False, "write": True,
            }],
        )],
    )
    intent = TestIntent(
        case_id="U001",
        obligation=TestObligation("ENTRY", "execution"),
        inputs={"param:data:address": 1},
        expected={"param:data:pointee:data[26]": 7},
        validation=ValidationResult(VALIDATED, checks=("oracle",)),
    )

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_intents_csv(
            ir, GenerationResult("indexed_target", VALIDATED, (intent,)),
        ))
    ))
    comment = next(row for row in rows if row and row[0] == "#COMMENT")
    data = next(row for row in rows if row and row[0] == "")

    assert comment.count("@data[26]") == 1
    assert data[comment.index("@data[26]")] == "0x7"


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


def test_csv_does_not_duplicate_input_only_stub_record_fields_as_outputs():
    ir = FunctionIR(
        name="stub_record_input", file="Dma.c", line=1, ret_type="void",
        calls=[CallSite(
            order=0, callee="read_record", line=2,
            params=[Param("record", "Record *", is_ptr=True)],
            caller_param_fields={"0": ["u1_data10"]},
            caller_param_output={"0": False},
            pointer_arguments={"0": {
                "address_used": True, "pointee_write": False,
            }},
        )],
    )

    rows = list(__import__("csv").reader(
        __import__("io").StringIO(render_csv(ir))
    ))
    comment = next(row for row in rows if row and row[0] == "#COMMENT")
    assert comment.count(
        "AMSTB_SrcFile.c/AMSTB_read_record@PTROUT00_read_record[0].u1_data10"
    ) == 1
    assert rows[0][4] == "1"


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


def test_render_switch_matrix_before_nested_branch_viewpoints():
    ir = FunctionIR(
        name="switch_target", file="Dma.c", line=1, ret_type="void",
        branches=[
            Branch(
                bid="B00", kind="switch", line=2,
                cond_text="switch (state)",
                cases=[Case("case 2", 2, False), Case("case 4", 4, False),
                       Case("default", None, True)],
            ),
            Branch(
                bid="B01", kind="if", line=3, parent_bid="B00",
                cond_text="if (state == 2)",
                atoms=[Atom("state", "uint8", "==", 2, None,
                             "state == 2")],
            ),
        ],
        globals_used=["state"],
        global_objects=[GlobalObject(name="state", read=True)],
        control_vars=[ControlVar("state", "state", "global", var_type="uint8")],
    )
    valid = ValidationResult(VALIDATED, checks=("test",))
    intents = (
        TestIntent(
            "U001", TestObligation("b0:case:0", "case", branch_id="B00",
                                   description="case 2:", case_label="case 2:"),
            inputs={"state": 2}, validation=valid,
        ),
        TestIntent(
            "U002", TestObligation("b0:case:1", "case", branch_id="B00",
                                   description="case 4:", case_label="case 4:"),
            inputs={"state": 4}, validation=valid,
        ),
        TestIntent(
            "U003", TestObligation("b0:case:2", "case", branch_id="B00",
                                   description="default:", case_label="default:"),
            inputs={"state": 0}, validation=valid,
        ),
        TestIntent(
            "U004", TestObligation("b1:T", "branch", branch_id="B01",
                                   outcome=True, description="if (state == 2)"),
            inputs={"state": 2}, validation=valid,
        ),
    )

    text = render_intents_csv(ir, GenerationResult("switch_target", VALIDATED, intents))
    labels = [line for line in text.splitlines() if line.startswith(";$L$")]
    assert labels.index(";$L$,default:") < labels.index(";$L$,if (state == 2)")
    assert labels.count(";$L$,if (state == 2)") == 1




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




def test_clang_keeps_external_const_array_member_as_test_input(tmp_path):
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
    assert ir.branches[0].constant_value is None
    control = next(cv for cv in ir.control_vars if "is_enable_dma" in cv.var)
    assert control.constant_value is None
    csv_text = render_csv(ir)
    assert "FALSE デッドコードがあった為、この分岐に入ることができません" not in csv_text


def test_csv_renders_null_pointer_address_zero_and_empty_pointee():
    ir = FunctionIR(
        name="target",
        file="target.c",
        line=1,
        ret_type="void",
        params=[Param("ptr", "uint8 *", is_ptr=True, is_written=True,
                      access_paths=["*ptr"])],
        branches=[Branch(
            bid="b0", kind="if", line=2, cond_text="ptr != 0",
            atoms=[Atom("ptr", "uint8 *", "!=", 0, "0", "ptr != 0")],
        )],
        calls=[CallSite(
            order=0, callee="helper", line=3,
            guards=[{"bid": "b0", "then": True}],
        )],
        control_vars=[ControlVar("ptr", "ptr", "param")],
    )
    result = GenerationResult(
        "target", VALIDATED,
        intents=(
            TestIntent(
                case_id="U001",
                obligation=TestObligation(oid="b0:T", kind="branch", branch_id="b0", outcome=True),
                inputs={"ptr": 1, "param:ptr:address": 1, "*ptr": 42, "call:helper:count": 0},
                expected={"*ptr": 42, "call:helper:count": 1},
                validation=ValidationResult("VALIDATED"),
            ),
            TestIntent(
                case_id="U002",
                obligation=TestObligation(oid="b0:F", kind="branch", branch_id="b0", outcome=False),
                inputs={"ptr": 0, "param:ptr:address": 0, "*ptr": 0, "call:helper:count": 0},
                expected={"call:helper:count": 0},
                validation=ValidationResult("VALIDATED"),
            ),
        ),
    )
    csv_text = render_intents_csv(ir, result)
    lines = [line.split(",") for line in csv_text.splitlines() if line.startswith(",")]
    assert len(lines) == 2
    true_row = lines[0]
    assert "0x5400" in true_row
    assert "0x2a" in true_row
    false_row = lines[1]
    assert "0x0" in false_row
    comment_line = next(line.split(",") for line in csv_text.splitlines() if line.startswith("#COMMENT"))
    ptr_val_idx = comment_line.index('"*ptr"')
    assert false_row[ptr_val_idx] == ""

