"""M1 golden 回归：CanIf_SetPduMode 解析结果必须与手写规格一致。

对照物：examples/golden/CanIf_SetPduMode/testdata.csv（B01..B09 手工登记）
与 docs/用例表与CSV格式规格.md。改动期望值须说明拍板出处。
"""
import os
from pathlib import Path

import pytest

from ut_agent.parser import ClangExtractor, default_clang_extractor, make_compile_context

ROOT = Path(__file__).resolve().parents[1]
CP = Path(os.environ.get("UT_AGENT_CLASSIC_PLATFORM",
                        str(ROOT / "examples" / "classic-platform")))
CFG = ROOT / "examples" / "configs" / "cp_canif"
SRC = CP / "communication" / "CanIf" / "src" / "CanIf.c"


def build_ir(function="CanIf_SetPduMode"):
    if not SRC.is_file():
        pytest.skip(
            "缺少外部 Classic Platform 基准源码；请设置 UT_AGENT_CLASSIC_PLATFORM "
            "或准备 examples/classic-platform/"
        )
    includes = [CP / "include", CP / "include" / "generic", CP / "base" / "compiler",
                CP / "drivers" / "CanTrcv", CFG, CFG / "libc_stub"]
    includes += sorted(p for p in CP.rglob("inc") if p.is_dir())
    defines = {"CANIF_CHANNEL_CNT": "2"}  # 其余配置在 CanIf_Cfg.h 内
    context = make_compile_context([SRC], includes, defines)
    return ClangExtractor(default_clang_extractor()).extract(
        context, function, cwd=SRC.parent
    )


@pytest.fixture(scope="module")
def ir():
    return build_ir()


def test_signature(ir):
    assert ir.ret_type == "Std_ReturnType"
    assert [(p.name, p.type) for p in ir.params] == [
        ("ControllerId", "uint8"),
        ("PduModeRequest", "CanIf_PduSetModeType"),
    ]


def test_stub_set(ir):
    """调用集 = 仅 Det_ReportError（DEV_ERROR_DETECT=1，PN/TX_BUFFERING=0）。"""
    assert [c.callee for c in ir.calls] == ["Det_ReportError"]
    assert ir.calls[0].via_macro == "DET_REPORT_ERROR" or \
        ir.calls[0].via_macro == "VALIDATE_RV"


def test_globals_and_locals(ir):
    assert ir.globals_used == ["CanIf_Global"]
    assert {"ret", "currMode", "newMode"} <= set(ir.locals)


def test_branch_counts(ir):
    """if 系 15 个（8 if + 7 elseif）+ 1 个 switch（7 case + default）。"""
    kinds = {}
    for b in ir.branches:
        key = b.kind if b.kind != "elseif" else "if"
        kinds[key] = kinds.get(key, 0) + 1
    assert kinds.get("if", 0) == 15
    assert kinds.get("switch", 0) == 1
    sw = next(b for b in ir.branches if b.kind == "switch")
    labels = {c.label for c in sw.cases}
    assert {"CANIF_SET_OFFLINE", "CANIF_SET_ONLINE", "CANIF_SET_TX_OFFLINE_ACTIVE"} <= labels
    assert any(c.is_default for c in sw.cases)
    assert sorted(c.value for c in sw.cases if not c.is_default) == [0, 1, 2, 3, 4, 5, 6]


def test_validate_rv_macros(ir):
    """两个 VALIDATE_RV 宏校验分支被识别并标记来源。"""
    macro_branches = [b for b in ir.branches if b.from_macro == "VALIDATE_RV"]
    assert len(macro_branches) == 2
    flat = [a for b in macro_branches for a in b.atoms]
    assert any(a.boundary == 1 and "initRun" in a.var for a in flat)   # TRUE == initRun
    assert any(a.boundary == 2 and a.op == "<" for a in flat)          # ControllerId < 2


def test_atoms_total_and_values(ir):
    """共 20 个原子条件；关键枚举真值正确展开。"""
    atoms = [a for b in ir.branches for a in b.atoms]
    assert len(atoms) == 20

    def has(boundary, name=None, op="=="):
        return any(a.boundary == boundary and (name is None or a.boundary_name == name)
                   and a.op == op for a in atoms)

    assert has(3, "CANIF_GET_ONLINE")
    assert has(2, "CANIF_GET_TX_ONLINE")
    assert has(1, "CANIF_GET_RX_ONLINE")
    assert has(5, "CANIF_GET_OFFLINE_ACTIVE_RX_ONLINE")
    assert has(4, "CANIF_GET_OFFLINE_ACTIVE")


def test_config_recorded(ir):
    assert ir.config.get("CANIF_CHANNEL_CNT") == "2"


def test_other_functions_smoke():
    """通用性冒烟：同文件任意函数都能独立抽取（函数级容忍，不要求全文件干净）。
    注意：上游函数的未恢复错误可能截断其后函数的 AST，冒烟对象取现存清单内函数。"""
    mode = build_ir("CanIf_GetControllerMode")   # 带指针出力引数的函数
    assert mode.ret_type == "Std_ReturnType"
    assert [p.name for p in mode.params] == ["ControllerId", "ControllerModePtr"]
    assert any(p.is_ptr for p in mode.params)
    assert [c.callee for c in mode.calls] == ["Det_ReportError"]

    getter = build_ir("CanIf_GetPduMode")
    assert getter.ret_type == "Std_ReturnType"
    assert any(b.from_macro == "VALIDATE_RV" for b in getter.branches)
