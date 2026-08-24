"""M2：stub 源码生成（确定性，规格 §3）。

命名：callcnt<k> / ARG<k>_<形参名> / PTIN<k>_<形参名> / PTOUT<k>_<形参名>[CALL_MAX]
/ CALLRET<k>[CALL_MAX]（仅当该返回值参与被测函数分支判定时生成——v0 依据
控制变量 source=="stub" 判定，flow 集成前保守不生成）。
stub 体无逻辑：先记录后递增，索引从 0 起。
"""
from __future__ import annotations

from ut_agent.ir import FunctionIR, is_scalar_type

CALL_MAX_DEFAULT = 16


def _base_type(ptr_type: str) -> str:
    """剥掉一层指针与 const：'const T *' → 'T'；'const T **' → 'T *'（指向物类型）。"""
    t = " ".join(ptr_type.replace("*", " * ").split())
    t = " ".join(w for w in t.split() if w != "const")
    if t.endswith("*"):
        t = t[:-1].strip()
    return t or "int"


def table_stub_name(call) -> str:
    """指针表安装 stub 的函数名（分发表成员 / 回调数组）。"""
    k = f"{call.order:02d}"
    if call.table_member:
        return f"stub{k}_{call.table_base}_{call.table_member}"
    return f"stub{k}_{call.table_base}"


def render_stub_c(ir: FunctionIR, call_max: int = CALL_MAX_DEFAULT,
                  with_prelude: bool = True) -> str:
    """with_prelude=False：嵌入 host 执行器单 TU 用（不带文件头注释与 #include）。"""
    out: list[str] = []
    if with_prelude:
        out += [
            "/*",
            f" * 自动生成 stub —— 被测函数: {ir.name}",
            f" * 源码: {ir.file} L{ir.line}",
            " *",
            " * 规则: 被测函数内所有调用函数 stub 化; 只留 callcnt 与引数入出力; 无逻辑",
            " * 命名: stub 编号 k 按调用顺序从 00 起; ARG<k>_<形参名>=入力记录;",
            " *       PTIN<k>_<形参名>=传入指针记录; PTOUT<k>_<形参名>[CALL_MAX]=传出设定;",
            " *       CALLRET<k>[CALL_MAX]=返回值(仅参与分支判定时生成)",
            " */",
            '#include "Std_Types.h"',
            "",
        ]
    out.append(f"#define CALL_MAX {call_max}  /* 单用例内单 stub 最大调用次数上限, 可配置 */")
    if with_prelude:
        out.append("")
    for call in ir.calls:
        k = f"{call.order:02d}"
        ptr_params = [p for p in call.params if p.is_ptr]
        out.append(f"/* ---- 调用#{k}: {call.callee} ---- */")
        if call.ptr_call:
            if not call.table_base:
                out.append("/* 指针表全局信息不足，未生成（介入点） */")
                out.append("")
                continue
            name = table_stub_name(call)
            sig = ", ".join(f"{t} arg{i}" for i, t in enumerate(call.arg_types)) or "void"
            out.append(f"uint32 callcnt{k} = 0;   /* 每用例执行前置 0 */")
            for i, t in enumerate(call.arg_types):
                if is_scalar_type(t, ir.enums):
                    out.append(f"{t} ARG{k}_arg{i}[CALL_MAX];  /* 入力记录 */")
            out.append(f"static void {name}({sig})   /* 安装至 {call.table_base}"
                       f"{'.' + call.table_member if call.table_member else '[]'} */")
            out.append("{")
            for i, t in enumerate(call.arg_types):
                if is_scalar_type(t, ir.enums):
                    out.append(f"    ARG{k}_arg{i}[callcnt{k}] = arg{i};")
            out.append(f"    callcnt{k}++;   /* 先记录后递增, 索引从 0 起 */")
            out.append("}")
            out.append("")
            continue
        out.append(f"uint32 callcnt{k} = 0;   /* 每用例执行前置 0 */")
        ptin_scalar, ptout_scalar = {}, {}
        for p in call.params:
            if p.is_ptr:
                elem = _base_type(p.type)
                if not is_scalar_type(elem, ir.enums):
                    out.append(f"/* {p.name}: 指向物为结构体（含 const 成员），v0 不记录/不设定 */")
                    continue
                if p.is_const:
                    ptin_scalar[p.name] = elem
                    out.append(f"{elem} PTIN{k}_{p.name}[CALL_MAX];  /* 传入指针: 记录指向物值 */")
                else:
                    ptout_scalar[p.name] = elem
                    out.append(f"{elem} PTOUT{k}_{p.name}[CALL_MAX];  /* 传出: 按调用序设定的写出值 */")
            else:
                out.append(f"{p.type} ARG{k}_{p.name}[CALL_MAX];  /* 入力记录 */")
        has_ret = call.ret_type not in ("void", "")
        # CALLRET 仅当返回值参与被测函数分支判定（规格 §7-7）；
        # v0 粗判定：存在 source=="stub" 的控制变量才生成，否则返回类型零值
        ret_controls = any(cv.source == "stub" for cv in ir.control_vars)
        if has_ret and ret_controls:
            out.append(f"{call.ret_type} CALLRET{k}[CALL_MAX];  /* 返回值设定: 按调用序 */")
        out.append("")
        sig_params = ", ".join(f"{p.type} {p.name}" for p in call.params)
        ret = call.ret_type if has_ret else "void"
        static_prefix = "static " if call.is_static else ""
        out.append(f"{static_prefix}{ret} {call.callee}({sig_params})")
        out.append("{")
        for p in call.params:
            if not p.is_ptr:
                out.append(f"    ARG{k}_{p.name}[callcnt{k}] = {p.name};")
            elif p.is_const and p.name in ptin_scalar:
                out.append(f"    PTIN{k}_{p.name}[callcnt{k}] = *{p.name};")
        if ptout_scalar:
            out.append(f"    if (callcnt{k} < CALL_MAX) {{")
            for p in call.params:
                if not p.is_const and p.name in ptout_scalar:
                    out.append(f"        *{p.name} = PTOUT{k}_{p.name}[callcnt{k}];")
            out.append("    }")
        out.append(f"    callcnt{k}++;   /* 先记录后递增, 索引从 0 起 */")
        if has_ret:
            if ret_controls:
                out.append(f"    return CALLRET{k}[callcnt{k} - 1];")
            else:
                out.append(f"    return 0;   /* 返回值未参与分支判定: 不加 CALLRET, 返回类型零值 */")
        out.append("}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
