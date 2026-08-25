"""WinAMS 原生 stub / TestCsv 契约测试。

这些测试不依赖外部 ECU 源码，确保主生成入口不会回到项目早期的
``callcnt00`` / ``case_id`` 自定义格式。
"""
from pathlib import Path

from ut_agent.ir import Atom, Branch, CallSite, ControlVar, FunctionIR, Param
from ut_agent.stub.generate import render_stub_c
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
