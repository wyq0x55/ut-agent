"""M2：用例表 CSV 渲染（确定性，规格 §4）。

区块：[A] stub 区 → [B] 被测引数 → [C] 被测返回值 → [D] 控制变量/写回期待。
行类型：`#` 分支注释行、`%` 组合分支行（&& 去全 F、|| 去全 T）、数据行。
设定列由脚本枚举；期待/记录列填 `?`，由执行（M2.5 host / WinAMS）回填。
"""
from __future__ import annotations

from itertools import product
from pathlib import Path

from ut_agent.cases import boundary
from ut_agent.ir import FunctionIR, is_scalar_type


def build_columns(ir: FunctionIR, cand: dict) -> list:
    """列模型：[{"header", "kind"(set/expect/record), "values", "enum", "cv"}] 按规格顺序。
    指针引数：const T*（传入）→ 指向物设定列；T*（传出）→ <名>_out 期待列，
    并登记 @地址 行（规格 §4.1：@引数名 分配地址，其前地址段空闲可用）。"""
    cols: list = []
    for call in ir.calls:
        k = f"{call.order:02d}"
        if call.ptr_call:
            cols.append({"header": f"callcnt{k}(期待·指针表)", "kind": "expect", "values": None})
            for i, t in enumerate(call.arg_types):
                if is_scalar_type(t, ir.enums):
                    cols.append({"header": f"ARG{k}_arg{i}(记录)", "kind": "record",
                                 "values": None})
            continue   # 函数指针调用：stub 经安装接入（ARG 列仅标量实参）
        cols.append({"header": f"callcnt{k}(期待)", "kind": "expect", "values": None})
        for p in call.params:
            if p.is_ptr and p.is_const:
                cols.append({"header": f"PTIN{k}_{p.name}(记录)", "kind": "record", "values": None})
            elif p.is_ptr:
                cols.append({"header": f"PTOUT{k}_{p.name}(设定)", "kind": "set", "values": [0]})
            else:
                cols.append({"header": f"ARG{k}_{p.name}(记录)", "kind": "record", "values": None})
        # CALLRET：仅当返回值参与分支判定（source=stub）；flow 接入前不生成
    for p in ir.params:
        if p.is_ptr and "**" not in p.type.replace(" ", ""):
            if p.is_const:
                cols.append({"header": f"{p.name}(设定)", "kind": "set", "values": [0],
                             "cv": None, "ptr_param": p})
            else:
                cols.append({"header": f"{p.name}_out(期待)", "kind": "expect", "values": None,
                             "cv": None, "ptr_param": p})
            continue
        c = cand.get(p.name)
        cols.append({"header": f"{p.name}(设定)", "kind": "set",
                     "values": sorted(c["values"]) if c else [0],
                     "enum": c["enum"] if c else {},
                     "cv": c["cv"] if c else None})
    cols.append({"header": "ret(期待)", "kind": "expect", "values": None})
    for name, c in cand.items():
        if c["cv"].source == "param":
            continue
        cols.append({"header": f"{name}(设定)", "kind": "set",
                     "values": sorted(c["values"]), "enum": c["enum"], "cv": c["cv"]})
    for w in ir.global_writes:
        key = w.replace(" ", "")
        last = key.split(".")[-1].split("[")[0]
        cols.append({"header": f"{last}_after(期待)", "kind": "expect", "values": None})
    return cols


def _fmt(v, col) -> str:
    if col.get("enum"):
        name = col["enum"].get(v)
        if name:
            return f"{v}({name})"
    values = col.get("values") or []
    cv = col.get("cv")
    vt = (cv.var_type or "") if cv else ""
    # boolean 标注：类型为 boolean，或值域整体 ⊆ {0,1}（旧版绑定把 boolean 归一成 int）
    is_bool = vt == "boolean" or (
        not col.get("enum") and len(values) >= 2 and set(values) <= {0, 1})
    if is_bool:
        return f"{v}({'TRUE' if v else 'FALSE'})"
    if values and v == max(values) and len(values) > 1 \
            and col.get("enum") and v not in col["enum"]:
        return f"{v}(非法值=max{max(u for u in values if u in col['enum'])}+1)"
    return str(v)


def _branch_comment(b) -> str:
    parts = [f"# {b.bid}", b.kind]
    if b.chain_index:
        parts.append(f"elseif链{b.chain_index}")
    if b.from_macro:
        parts.append(f"来自宏 {b.from_macro}")
    cond = b.cond_text if b.cond_text else "(switch)"
    parts.append(cond if len(b.atoms) != 1 else b.atoms[0].text)
    for a in b.atoms:
        t = f"{a.var_type}" if a.var_type else "?"
        val = f"{a.boundary}({a.boundary_name})" if a.boundary_name else f"{a.boundary}"
        parts.append(f"[{a.var.replace(' ', '')}:{t} {a.op} {val}]")
    return " | ".join(str(p) for p in parts)


def _combination_rows(b) -> list:
    rows = []
    if b.kind == "switch":
        for c in b.cases:
            if c.is_default:
                rows.append("% default(其他值)")
            else:
                rows.append(f"% case {c.label}({c.value})")
        return rows
    n = len(b.atoms)
    if n == 0:
        return rows
    if n == 1:
        return ["% True", "% False"]
    combos = []
    for tup in product("TF", repeat=n):
        if b.connective == "||" and all(t == "T" for t in tup):
            continue   # || 不列全 T 行
        if b.connective == "&&" and all(t == "F" for t in tup):
            continue   # && 不列全 F 行
        combos.append(" ".join(tup))
    return [f"% {c}" for c in combos]


def render_csv(ir: FunctionIR, cfg_display: str) -> str:
    cand = boundary.control_candidates(ir)
    cols = build_columns(ir, cand)
    _, rows = boundary.enumerate_rows(ir)

    out: list[str] = []
    params_sig = " ; ".join(f"{p.type} {p.name}" for p in ir.params)
    out.append(f"# CFG: {cfg_display}")
    out.append(f"# TARGET: {ir.ret_type} {ir.name}({params_sig}) @ "
               f"{Path(ir.file).name} L{ir.line}")
    out.append("# 列语义: (设定)=脚本枚举 ; (期待)/(记录)=执行后回填(M2.5 host / WinAMS)")
    # 指针引数的 @地址 行（规格 §4.1）
    for p in ir.params:
        if p.is_ptr:
            out.append(f"# @{p.name} = 0x1000 ; 其前地址段空闲, 可用作指向物数据区")
    out.append("")
    for b in ir.branches:
        out.append(_branch_comment(b))
        out.extend(_combination_rows(b))
    out.append(",".join(["case_id"] + [c["header"] for c in cols]))
    for i, row in enumerate(rows, 1):
        cells = [f"U{i:03d}"]
        for col in cols:
            if col["kind"] == "set":
                cells.append(_fmt(row[col["header"].split("(")[0]], col))
            else:
                cells.append("?")
        out.append(",".join(cells))
    # 覆盖核对脚注
    notes = []
    for col in cols:
        if col["kind"] == "set" and col.get("values"):
            vals = ",".join(str(v) for v in col["values"])
            notes.append(f"{col['header'].split('(')[0]}:{{{vals}}}")
    out.append(f"# 边界五点覆盖(按值域): {' ; '.join(notes)}")
    out.append("# 组合行规则: || 不列 T T 行 ; && 不列 F F 行 (规格 §4.2)")
    for n in ir.notes:
        out.append(f"# note: {n}")
    return "\n".join(out) + "\n"
