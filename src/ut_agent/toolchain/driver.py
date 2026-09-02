"""M2.5：driver 生成（确定性，列模型驱动）。

- 用例数组与 enumerate_rows 同序（行号即 CSV 行号）
- 设定：值引数 → 局部变量；const 指针引数 → 指向物局部变量取址传入（@地址 约定，
  host 实现为栈地址）；global 控制变量 → 直接赋值；local_from_global → 赋值其来源全局
  （含数组下标越界保护）
- 出力：实际写回指针引数（Param.is_written）→ <名>_out 回读；全局写回 → *_after；返回值 → ret
- 打印列序与 CSV 列模型的 期待/记录 列完全一致
"""
from __future__ import annotations

from ut_agent.ir import FunctionIR, TypeInfo


class UnsupportedGen(Exception):
    """v0 不支持的生成形态（批量运行时归类 FAIL_GEN，归入 flow/LLM 介入点素材）"""


def _norm(text: str) -> str:
    return (text or "").replace(" ", "")


def _numeric_base(type_info: TypeInfo | None) -> bool:
    return bool(
        type_info
        and type_info.kind == "pointer"
        and type_info.pointee_info
        and type_info.pointee_info.is_scalar
    )


def _pointee_type(param) -> str:
    if param.type_info is None or not param.type_info.pointee_type:
        raise UnsupportedGen(f"形参 {param.name} 缺少 typed pointee fact")
    return param.type_info.pointee_type.replace("const ", "").strip()


def render_driver(ir: FunctionIR, columns, cols, rows,
                  guard_param: str = "ControllerId",
                  guard_bound: str = "CANIF_CHANNEL_CNT") -> str:
    """columns: csv_render.build_columns 输出；cols/rows: enumerate_rows 输出。"""
    if any(c.ptr_call and not c.table_base for c in ir.calls):
        raise UnsupportedGen("存在无法定位指针表的调用（表全局信息不足）：LLM/手工介入点")
    returns_void = ir.ret_type.strip() == "void"
    settable = {name: cv for name, cv, _ in cols}
    param_types = {p.name: p.type for p in ir.params}

    # 指针表 stub 安装（一次性，主循环前）
    installs = []
    for call in ir.calls:
        if not call.ptr_call or not call.table_base:
            continue
        from ut_agent.targets.winams.stub import table_stub_name
        name = table_stub_name(call)
        if call.table_member:
            installs.append(
                f"    ut_agent_fix_{call.table_base}.{call.table_member} = {name};")
        else:
            installs.append(
                f"    for (int j = 0; j < UT_FIX_{call.table_base}_N; j++) "
                f"ut_agent_fix_{call.table_base}[j] = {name};")

    setup: list[str] = []
    call_args = {p.name: p.name for p in ir.params}
    print_expr: dict[str, str] = {}     # header → C 表达式（int 可打印）

    # 指针引数：指向物局部变量 + 取址（数值型指向物取用例值，结构体指向物零初始化）
    for p in ir.params:
        if not p.is_ptr:
            continue
        if p.type_info is not None and p.type_info.pointer_depth >= 2:
            raise UnsupportedGen(f"双重指针形参 {p.name}: {p.type}")
        base = _pointee_type(p)
        if _numeric_base(p.type_info):
            init = f"({base})c->{p.name}" if not p.is_written else "0"
            setup.append(f"        {base} v_{p.name} = {init};")
        else:
            setup.append(f"        {base} v_{p.name} = {{0}};   /* 结构体指向物: 零初始化 */")
        call_args[p.name] = f"&v_{p.name}"

    # 值引数局部变量
    for p in ir.params:
        if not p.is_ptr:
            setup.append(f"        {param_types[p.name]} {p.name} = "
                         f"({param_types[p.name]})c->{p.name};")

    def notes_note(msg: str) -> None:
        ir.notes.append(f"[driver] {msg}")

    for name, cv in settable.items():
        if cv is None or cv.source not in ("global", "local_from_global"):
            continue
        expr = _norm(cv.var if cv.source == "global" else cv.set_via)
        cast = (
            f"({cv.type_info.canonical_type})"
            if cv.type_info and cv.type_info.canonical_type else ""
        )
        origin = cv.value_origin
        if origin is not None and origin.index == guard_param:
            setup.append(f"        if ({guard_param} < {guard_bound}) {{ {expr} = "
                         f"{cast}c->{name}; }}")
        else:
            setup.append(f"        {expr} = {cast}c->{name};")

    # 打印表达式（期待/记录列）
    arg_ref = {}
    for call in ir.calls:
        k = f"{call.order:02d}"
        if call.ptr_call:
            for i, t in enumerate(call.arg_types):
                if i < len(call.arg_type_infos) and call.arg_type_infos[i] \
                        and call.arg_type_infos[i].is_scalar:
                    arg_ref[f"ARG{k}_arg{i}(记录)"] = f"(int)ARG{k}_arg{i}[0]"
            continue
        for p in call.params:
            if not p.is_ptr:
                arg_ref[f"ARG{k}_{p.name}(记录)"] = f"(int)ARG{k}_{p.name}[0]"
    out_print_cols = [c for c in columns if c["kind"] in ("expect", "record")]
    for col in out_print_cols:
        h = col["header"]
        if h.startswith("callcnt"):
            print_expr[h] = f"(int)callcnt{h[7:9]}"
        elif h in arg_ref:
            print_expr[h] = arg_ref[h]
        elif h == "ret(期待)":
            print_expr[h] = "0" if returns_void else "(int)ret"
        elif h.endswith("_out(期待)"):
            pname = h.split("(")[0][:-4]
            ptype = next((p.type for p in ir.params if p.name == pname), None)
            param = next((p for p in ir.params if p.name == pname), None)
            if param and _numeric_base(param.type_info):
                print_expr[h] = f"(int)v_{pname}"
            else:
                print_expr[h] = "0"
                notes_note(f"指针出力 {pname} 指向物为结构体，v0 打印 0（不展开）")
        else:   # *_after：取第一个全局写回
            if not ir.global_writes:
                print_expr[h] = "0"
            else:
                print_expr[h] = "after"
    if ir.global_writes:
        write_effect = next(iter(ir.global_write_effects), None)
        after_expr = _norm(
            write_effect.path if write_effect is not None else ir.global_writes[0]
        )
        fallback = next((cv.name for cv in ir.control_vars
                         if cv.source == "local_from_global"), None)
        if (write_effect is not None and write_effect.origin is not None
                and write_effect.origin.index == guard_param):
            alt = f"(int)c->{fallback}" if fallback else "0"
            after_stmt = (f"        int after = ({guard_param} < {guard_bound}) ? "
                          f"(int)({after_expr}) : {alt};")
        else:
            after_stmt = f"        int after = (int)({after_expr});"
    else:
        after_stmt = "        int after = 0;"

    # 结构体与用例数组（列序 = settable 顺序）
    case_fields = list(settable.keys())
    case_struct = "typedef struct { int " + "; int ".join(case_fields) + "; } CaseRow;"
    case_init = ", ".join("{" + ", ".join(str(row[n]) for n in case_fields) + "}"
                          for row in rows)
    call_stmt = (f"        {ir.ret_type} ret = {ir.name}("
                 + ", ".join(call_args[p.name] for p in ir.params) + ");"
                 ) if not returns_void else (
                 f"        {ir.name}("
                 + ", ".join(call_args[p.name] for p in ir.params) + ");")

    fmt_normal = ",".join("%d" for _ in out_print_cols)
    args_normal = ", ".join(print_expr[c["header"]] for c in out_print_cols)
    reset_stmts = [f"        callcnt{c.order:02d} = 0;" for c in ir.calls] or ["        /* 无 stub */"]

    lines = [
        "/* ===== driver（自动生成） ===== */",
        "#include <stdio.h>",
        case_struct,
        f"static const CaseRow CASES[{len(rows)}] = {{ {case_init} }};",
        "",
        "int main(void)",
        "{",
        *installs,
        f"    for (int i = 0; i < {len(rows)}; i++) {{",
        "        const CaseRow* c = &CASES[i];",
        *setup,
        *reset_stmts,
        call_stmt,
        after_stmt,
        f"        printf(\"{fmt_normal}\\n\", {args_normal});",
        "        /* callcnt=0 时 ARG/PTIN 为陈旧值，解析端按 callcnt 判定忽略 */",
        "    }",
        "    return 0;",
        "}",
    ]
    return "\n".join(lines)
