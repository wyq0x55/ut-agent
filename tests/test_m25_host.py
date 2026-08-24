"""M2.5 闭环验收：host 执行器实跑 480 用例，回填结果与手写 golden 逐条比对。

golden 手写的 17 行期待值（ret / PduMode_after / callcnt / ARG 记录）是人工推导的；
本测试证明：抽取 + stub + driver 编译执行得到的**实测值**与之一致。
"""
import re

import pytest

from conftest import CP, CFG, DEFINES, ROOT, build_tu_ir
from ut_agent.cases import boundary
from ut_agent.host import driver as driver_gen
from ut_agent.host import extract, run
from ut_agent.winams.csv_render import render_csv

GOLDEN = ROOT / "examples" / "golden" / "CanIf_SetPduMode"
BUILD = ROOT / ".build" / "host"


def _int(cell: str):
    m = re.match(r"-?\d+", cell.strip())
    return int(m.group()) if m else None


def _repo_defines() -> dict:
    """DET 上报 ID 的真实值来自仓库头（CanIf.h / CanIf_Types.h）。"""
    import re as _re
    out = {}
    for f in ("communication/CanIf/inc/CanIf.h",
              "communication/CanIf/inc/CanIf_Types.h"):
        text = (CP / f).read_text(encoding="utf-8")
        for m in _re.findall(r"#define\s+(CANIF_\w+)\s+(\S+)", text):
            try:
                out[m[0]] = int(m[1].rstrip("uU"), 0)
            except ValueError:
                continue     # 续行宏等非整数值，跳过
    return out


@pytest.fixture(scope="module")
def executed():
    tu, ir = build_tu_ir()
    from ut_agent.winams.csv_render import build_columns
    cols, rows = boundary.enumerate_rows(ir)
    columns = build_columns(ir, boundary.control_candidates(ir))
    driver_code = driver_gen.render_driver(ir, columns, cols, rows)
    source = extract.build_harness_source(tu, ir, driver_code)
    BUILD.mkdir(parents=True, exist_ok=True)
    c_file = BUILD / "harness.c"
    c_file.write_text(source, encoding="utf-8")
    # WSL gcc：用系统真头文件（不含 libc_stub），配置注入目录保留
    includes = [CP / "include", CP / "include" / "generic", CP / "base" / "compiler",
                CP / "drivers" / "CanTrcv", CFG]
    includes += sorted(p for p in CP.rglob("inc") if p.is_dir())
    lines = run.compile_and_run(c_file, BUILD, includes, DEFINES)
    results = run.parse_result_lines(lines)
    return ir, cols, rows, results


def test_all_rows_executed(executed):
    _, _, rows, results = executed
    assert len(rows) == len(results) == 480


def test_full_table_semantics(executed):
    """全表语义抽查：校验失败路径 / 正常路径的行为规律。"""
    _, cols, rows, results = executed
    for row, res in zip(rows, results):
        if row["initRun"] == 0:
            assert res["callcnt"] == 1 and res["ret"] == 1   # B01 拦截
        elif row["ControllerId"] >= 2:
            assert res["callcnt"] == 1 and res["ret"] == 1   # B02 拦截
        else:
            assert res["callcnt"] == 0                       # 不触 DET


def test_golden_17_rows_expectations(executed):
    """手写 golden 的 17 行：实测值 == 人工推导期待值（含 DET 记录列）。"""
    _, cols, rows, results = executed
    cfg = _repo_defines()
    index = {(row["ControllerId"], row["PduModeRequest"], row["initRun"], row["currMode"]): res
             for row, res in zip(rows, results)}

    golden_lines = [l for l in (GOLDEN / "testdata.csv").read_text(encoding="utf-8").splitlines()
                    if re.match(r"^U\d+,", l)]
    assert len(golden_lines) == 17
    for line in golden_lines:
        cells = line.split(",")
        # 列序: case_id, callcnt, ARG00_ModuleId, InstanceId, ApiId, ErrorId, ControllerId,
        #       PduModeRequest, ret, initRun, currMode, PduMode_after
        cid = cells[0]
        exp_callcnt = _int(cells[1])
        res = index[(_int(cells[6]), _int(cells[7]), _int(cells[9]), _int(cells[10]))]
        assert res["callcnt"] == exp_callcnt, f"{cid} callcnt"
        assert res["ret"] == _int(cells[8]), f"{cid} ret"
        assert res["after"] == _int(cells[11]), f"{cid} PduMode_after"
        if exp_callcnt == 1:
            assert res["args"][0] == cfg["CANIF_MODULE_ID"], f"{cid} moduleId"
            assert res["args"][1] == 0, f"{cid} instanceId"
            assert res["args"][2] == cfg["CANIF_SETPDUMODE_ID"], f"{cid} apiId"
            expected_err = "CANIF_E_UNINIT" if cells[5].startswith("CANIF_E_UNINIT") \
                else "CANIF_E_PARAM_CONTROLLERID"
            assert res["args"][3] == cfg[expected_err], f"{cid} errorId"


def test_backfill_csv(executed):
    """渲染 CSV 的 ? 用实测值回填，抽查回填文本。"""
    ir, cols, rows, results = executed
    csv_text = render_csv(ir, "CANIF_CHANNEL_CNT=2 ; CANIF_PUBLIC_DEV_ERROR_DETECT=STD_ON ; "
                         "CANIF_PUBLIC_PN_SUPPORT=STD_OFF ; CANIF_PUBLIC_TX_BUFFERING=STD_OFF")
    lines = csv_text.splitlines()
    header_idx = next(i for i, l in enumerate(lines) if l.startswith("case_id,"))
    header = lines[header_idx].split(",")
    arg_order = [h for h in header if h.startswith("ARG")]   # ARG 列的参数序
    out = lines[:header_idx + 1]
    for i, (row, res) in enumerate(zip(rows, results), 1):
        cells = []
        for h in header[1:]:
            key = h.split("(")[0]
            if "(设定)" in h:
                cells.append(str(row[key]))
            elif h.startswith("callcnt"):
                cells.append(str(res["callcnt"]))
            elif h.startswith("ARG"):
                a = res["args"][arg_order.index(h)]
                cells.append(str(a) if a is not None else "-")
            elif h == "ret(期待)":
                cells.append(str(res["ret"]))
            else:
                cells.append(str(res["after"]))
        out.append(",".join([f"U{i:03d}"] + cells))
    filled = "\n".join(out)
    (BUILD / "filled_testdata.csv").write_text(filled, encoding="utf-8")
    # 首行精确定格：initRun=0 → B01 拦截，DET 记录 = 仓库真值(60,0,9,30)
    first = filled.splitlines()[header_idx + 1]
    assert first == "U001,1,60,0,9,30,0,0,1,0,0,0", first
    assert "?" not in filled.splitlines()[header_idx + 1]
