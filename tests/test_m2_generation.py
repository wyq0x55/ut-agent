"""M2 验收：stub 与用例表 CSV 的脚本生成结果对上手写 golden。

WinAMS CSV 格式契约见 ``docs/winams/coverage-csv.md``：
- stub 源码：代码部分（去注释去空白）与手写完全一致
- CSV：列头与手写完全一致；分支注释行/组合行结构一致；设定列全量枚举；
  期待/记录列填 ?（由 M2.5 执行回填——手写 golden 里是人工推导值）
"""
import re

from conftest import ROOT, build_ir
from ut_agent.generation import boundary
from ut_agent.targets.winams.stub import render_spec_stub_c
from ut_agent.targets.winams.csv import build_columns, render_spec_csv

GOLDEN = ROOT / "examples" / "golden" / "CanIf_SetPduMode"
CFG_DISPLAY = ("CANIF_CHANNEL_CNT=2 ; CANIF_PUBLIC_DEV_ERROR_DETECT=STD_ON ; "
               "CANIF_PUBLIC_PN_SUPPORT=STD_OFF ; CANIF_PUBLIC_TX_BUFFERING=STD_OFF")


def _norm(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return re.sub(r"\s+", "", text)


def test_stub_code_matches_golden():
    ir = build_ir()
    golden = (GOLDEN / "CanIf_SetPduMode_stubs.c").read_text(encoding="utf-8")
    # 旧 golden 仅回归 host 内部 fixture，不是 WinAMS 交付格式。
    assert _norm(render_spec_stub_c(ir)) == _norm(golden)


def test_csv_header_matches_golden():
    ir = build_ir()
    cand = boundary.control_candidates(ir)
    cols = build_columns(ir, cand)
    golden_header = next(line for line in
                         (GOLDEN / "testdata.csv").read_text(encoding="utf-8").splitlines()
                         if line.startswith("case_id,"))
    assert ",".join(["case_id"] + [c["header"] for c in cols]) == golden_header


def test_csv_branch_rows():
    csv_text = render_spec_csv(build_ir(), CFG_DISPLAY)
    b_lines = [line for line in csv_text.splitlines() if line.startswith("# B")]
    assert len(b_lines) == 16                                # 15 if/elseif + 1 switch
    assert sum("VALIDATE_RV" in line for line in b_lines) == 2
    assert any("| switch |" in line for line in b_lines)
    # switch 组合行：7 case + default
    assert "% case CANIF_SET_OFFLINE(0)" in csv_text
    assert "% case CANIF_SET_TX_OFFLINE_ACTIVE(6)" in csv_text
    assert "% default(其他值)" in csv_text
    # || 分支（2 原子）组合行 3 行且无 T T
    or_blocks = [line for line in b_lines if "||" in line]
    assert or_blocks
    lines = csv_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# B") and "||" in line:
            combo = []
            j = i + 1
            while j < len(lines) and lines[j].startswith("%"):
                combo.append(lines[j])
                j += 1
            assert len(combo) == 3, f"{line} 下组合行应 3 行: {combo}"
            assert "% T T" not in combo


def test_enumeration_rows():
    ir = build_ir()
    cols, rows = boundary.enumerate_rows(ir)
    assert len(rows) == 480   # ControllerId(5) × PduModeRequest(8) × initRun(2) × currMode(6)

    def col_vals(name):
        return {row[name] for row in rows}

    assert col_vals("ControllerId") == {0, 1, 2, 3, 255}
    assert col_vals("PduModeRequest") == set(range(0, 8))
    assert col_vals("initRun") == {0, 1}
    assert col_vals("currMode") == set(range(0, 6))
    # 手写 golden 的设定组合是全量的子集
    def has(**kw):
        return any(all(row[k] == v for k, v in kw.items()) for row in rows)
    assert has(ControllerId=0, PduModeRequest=0, initRun=1, currMode=0)   # U01
    assert has(ControllerId=2, PduModeRequest=0, initRun=1, currMode=0)   # U14
    assert has(ControllerId=0, PduModeRequest=7, initRun=1, currMode=0)   # U12
    assert has(ControllerId=0, PduModeRequest=4, initRun=1, currMode=4)   # U10


def test_control_var_sources():
    ir = build_ir()
    src = {cv.name: cv.source for cv in ir.control_vars}
    assert src.get("ControllerId") == "param"
    assert src.get("PduModeRequest") == "param"
    assert src.get("initRun") == "global"
    assert src.get("currMode") == "local_from_global"
    cv = next(c for c in ir.control_vars if c.name == "currMode")
    assert "CanIf_Global" in (cv.set_via or "")
    assert ir.global_writes and all("CanIf_Global" in w for w in ir.global_writes)


def test_data_row_values_formatted():
    csv_text = render_spec_csv(build_ir(), CFG_DISPLAY)
    lines = csv_text.splitlines()
    header_idx = next(i for i, l in enumerate(lines) if l.startswith("case_id,"))
    data = [line for line in lines if re.match(r"^U\d+,", line)]
    assert len(data) == 480
    # 期待/记录列全部为 ?（待执行回填）；每行 12 列
    expect_record_pos = [i for i, h in enumerate(lines[header_idx].split(","))
                         if "(期待)" in h or "(记录)" in h]
    for line in data:
        cells = line.split(",")
        assert len(cells) == 12
        assert all(cells[i] == "?" for i in expect_record_pos)
    assert "0(SET_OFFLINE)" in csv_text          # 枚举名标注
    assert "7(非法值=max6+1)" in csv_text         # default 触发值标注
    assert "1(TRUE)" in csv_text                  # boolean 标注
