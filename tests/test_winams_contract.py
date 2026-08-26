"""WinAMS 原生 stub / TestCsv 契约测试。

这些测试不依赖外部 ECU 源码，确保主生成入口不会回到项目早期的
``callcnt00`` / ``case_id`` 自定义格式。
"""
from pathlib import Path

from ut_agent.ir import (
    Atom, Branch, CallSite, ControlVar, FunctionIR, MemoryVar, Param,
)
from ut_agent.parser import clang_parser
from ut_agent.stub.generate import render_stub_c
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
from ut_agent.winams.csv_render import render_csv


def _ir() -> FunctionIR:
    return FunctionIR(
        name="target",
        file="Dma.c",
        line=10,
        ret_type="uint8",
        params=[Param("value", "uint8"), Param("out", "uint8 *", is_ptr=True)],
        calls=[CallSite(
            order=0,
            callee="dep",
            line=12,
            params=[Param("arg", "uint16"), Param("result", "uint8 *", is_ptr=True)],
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


def test_csv_is_winams_testcsv():
    text = render_csv(_ir(), source_label="Dma.c/target")
    first, comment, branch = text.splitlines()[:3]
    assert first.startswith('mod,"Dma.c/target","target')
    assert first.split(",")[3:5] == ["6", "2"]
    assert comment.startswith("#COMMENT,")
    assert branch == ";$L$,value == 1"
    assert ";$L$,TRUE" in text
    assert ";$L$,FALSE" in text
    assert "case_id," not in text
    assert "\r\n" in text
    assert text.encode("cp932")


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


def test_ast_extracts_memory_mapped_address_and_access_width(tmp_path):
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
    tu = clang_parser.parse_tu(source, strict=True)
    ir = clang_parser.extract_function(tu, source, "target")

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


def test_ast_drops_write_only_io_when_high_level_output_exists(tmp_path):
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
    tu = clang_parser.parse_tu(source, strict=True)
    ir = clang_parser.extract_function(tu, source, "target")

    assert ir.memory_vars == []
    assert "U4L_DMA_REG_ICDMA04" not in render_csv(ir)
    assert "AMSTB_p_vol_dma_write_reg32" not in render_csv(ir)


def test_ast_propagates_const_array_member_and_marks_dead_branch(tmp_path):
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
    tu = clang_parser.parse_tu(source, [tmp_path], strict=True)
    config_tu = clang_parser.parse_tu(config, [tmp_path], strict=True)
    ir = clang_parser.extract_function(
        tu, source, "target", context_tus=(config_tu,)
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
