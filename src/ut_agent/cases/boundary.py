"""M2：控制变量候选值与用例行枚举（确定性，规格 §4.4）。

五点规则：{v−1, v, v+1} ∪ {可达域 min, max}；±1 超出值域不加；
switch 变量加 case 值全体 + default 触发值（max+1）。
"""
from __future__ import annotations

from itertools import product
from typing import Optional

from ut_agent.ir import FunctionIR

PAIRWISE_THRESHOLD = 500   # 超过则降 pairwise（v0 未实现，先告警备注）


def domain_of(var_type: Optional[str], enums: dict):
    """值域：("set", 值集合) 或 ("range", min, max) 或 None（未知，不过滤）。"""
    if not var_type:
        return None
    t = var_type.strip()
    if t == "boolean":
        return ("set", {0, 1})
    if t in enums:
        return ("set", set(enums[t].values()))
    if "uint8" in t:
        return ("set", set(range(256)))
    if "sint8" in t:
        return ("set", set(range(-128, 128)))
    if "uint16" in t:
        return ("range", 0, 65535)
    if "uint32" in t or "unsigned" in t:
        return ("range", 0, 4294967295)
    return None


def _in(v, dom) -> bool:
    if dom is None:
        return True
    if dom[0] == "set":
        return v in dom[1]
    return dom[1] <= v <= dom[2]


def _dmin(dom):
    return min(dom[1]) if dom[0] == "set" else dom[1]


def _dmax(dom):
    return max(dom[1]) if dom[0] == "set" else dom[2]


def five_points(boundary, dom) -> set:
    """边界五点。±1 必须能进该分支（值域合法）才保留；min/max 取值域极值。"""
    if boundary is None:
        return set()
    pts = set()
    for v in (boundary - 1, boundary, boundary + 1):
        if _in(v, dom):
            pts.add(v)
    if dom is not None:
        pts.add(_dmin(dom))
        pts.add(_dmax(dom))
    return pts


def _resolve_enum_domain(ir: FunctionIR, boundary_name) -> tuple | None:
    """var_type 缺失时，用边界里的枚举名反查它所属的枚举域。"""
    if not boundary_name:
        return None
    for name, members in ir.enums.items():
        if boundary_name in members:
            return ("set", set(members.values()))
    return None


def _const_domain(boundary_name) -> tuple | None:
    """标准常量宏隐含的值域（AUTOSAR：boolean / Std_ReturnType）。"""
    if boundary_name in ("TRUE", "FALSE", "E_OK", "E_NOT_OK"):
        return ("set", {0, 1})
    return None


def control_candidates(ir: FunctionIR) -> dict:
    """{控制变量短名: {"cv": ControlVar, "values": set, "enum": 反查表}}。"""
    var_to_cv = {cv.var.replace(" ", ""): cv for cv in ir.control_vars}
    cand: dict = {}
    acc: dict = {}

    def add(cv, values):
        c = cand.setdefault(cv.name, {"cv": cv, "values": set(),
                                      "enum": _reverse_enum(ir, cv.var_type)})
        c["values"] |= values

    for b in ir.branches:
        for a in b.atoms:
            cv = var_to_cv.get(a.var.replace(" ", ""))
            if cv is None:
                continue
            # Prefer the enum domain inferred from the actual boundary name.
            # libclang often exposes an enum typedef as its underlying type
            # (for example ``unsigned int``); using that type first would
            # incorrectly widen a six-value enum to 0..UINT_MAX.
            dom = (_resolve_enum_domain(ir, a.boundary_name)
                   or domain_of(cv.var_type, ir.enums)
                   or domain_of(a.var_type, ir.enums)
                   or _const_domain(a.boundary_name))
            if cv.name not in acc:
                acc[cv.name] = (cv, [])
            acc[cv.name][1].append((a.boundary, dom))
        if b.kind == "switch":
            cv = var_to_cv.get(b.cond_text.replace(" ", ""))
            if cv is None:
                continue
            vals = {c.value for c in b.cases if not c.is_default and c.value is not None}
            if vals:
                add(cv, vals)
                add(cv, {max(vals) + 1})          # default 触发（max+1）
                dom = domain_of(cv.var_type, ir.enums)
                if dom is not None:
                    add(cv, {_dmin(dom), _dmax(dom)})
    # 值域推断兜底：类型链全失败但全部边界值 ∈ {0,1} → 布尔域
    # （旧版绑定会把 boolean typedef 归一成 "int"，确定性补救）
    for name, (cv, items) in acc.items():
        if all(d is None for _, d in items) and all(bd in (0, 1, None) for bd, _ in items):
            acc[name] = (cv, [(bd, ("set", {0, 1})) for bd, _ in items])
    for cv, items in acc.values():
        for bd, dom in items:
            add(cv, five_points(bd, dom))
    return cand


def _reverse_enum(ir, var_type) -> dict:
    """{值: 成员短名}。短名 = 成员名去掉枚举类型首词的前缀
    （CanIf_PduSetModeType → CANIF_ 前缀 → SET_OFFLINE），与 golden 标注一致。"""
    if not var_type:
        return {}
    t = var_type.strip()
    if t not in ir.enums:
        return {}
    prefix = t.split("_")[0].upper() + "_"
    out = {}
    for k, v in ir.enums[t].items():
        out[v] = k[len(prefix):] if k.startswith(prefix) else k
    return out


def settable_columns(ir: FunctionIR, cand: dict) -> list:
    """设定列（有序）：[B] 引数 + [D] 可设定控制变量（global/local_from_global）。
    config（配置表只读）/ local / unknown 来源不生成设定列（notes 已记介入点）。"""
    cols = []
    for p in ir.params:
        c = cand.get(p.name)
        cols.append((p.name, c["cv"] if c else None,
                     sorted(c["values"]) if c else [0]))
    for name, c in cand.items():
        cv = c["cv"]
        if cv.source != "local_from_global" and cv.source != "global":
            continue
        cols.append((name, cv, sorted(c["values"])))
    return cols


def enumerate_rows(ir: FunctionIR, threshold: int = PAIRWISE_THRESHOLD):
    """全组合枚举；超过阈值降 pairwise（v0 记 TODO 备注，先笛卡尔）。"""
    cand = control_candidates(ir)
    cols = settable_columns(ir, cand)
    sizes = [len(v) for _, _, v in cols]
    total = 1
    for s in sizes:
        total *= s
    if total > threshold:
        ir.notes.append(f"组合数 {total} 超过阈值 {threshold}，应降 pairwise（v0 未实现，仍全量）")
    keys = [name for name, _, _ in cols]
    rows = [dict(zip(keys, combo))
            for combo in product(*(values for _, _, values in cols))]
    return cols, rows
