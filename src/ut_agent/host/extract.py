"""M2.5：函数抽取 → host 执行器单 TU。

抽取规则（对应方案的"抽取被测函数"步骤）：
  [源文件 1..首个函数定义前]（= 全部 include/类型/全局定义）
  + [目标函数行窗口]
  + stub 源码（嵌入模式）
  + driver（自动生成）
单文件编译执行，无外部链接依赖。
"""
from __future__ import annotations

from pathlib import Path

from ut_agent.ir import FunctionIR
from ut_agent.stub.generate import render_spec_stub_c


def _first_function_line(ir: FunctionIR) -> int:
    """Use source facts emitted by the C++ extractor when available."""
    return int(ir.extensions.get("first_function_line", ir.line) or ir.line or 1)


def _sanitize_pp(lines: list) -> list:
    """函数窗口预处理平衡：丢弃孤儿 #endif/#else/#elif（开在窗口外），
    为窗口内未闭合的 #if 补 #endif。harness 只需语义等价，不需条件编译原样。"""
    out = []
    depth = 0
    for s in lines:
        t = s.strip()
        if t.startswith(("#if", "#ifdef", "#ifndef")):
            depth += 1
            out.append(s)
        elif t.startswith("#endif"):
            if depth > 0:
                depth -= 1
                out.append(s)
        elif t.startswith(("#else", "#elif")):
            if depth > 0:
                out.append(s)
        else:
            out.append(s)
    out.extend(["#endif /* ut-agent 补闭合 */"] * depth)
    return out


def _prototypes(ir) -> list:
    """stub 目标的前置原型：目标函数窗口里对 static stub 的调用需要先见声明。"""
    out = ["/* ---- 前置原型（ut-agent 生成，保证调用先于 stub 定义可见） ---- */"]
    for call in ir.calls:
        if call.ptr_call:
            continue
        sig = ", ".join(f"{p.type} {p.name}" for p in call.params)
        static = "static " if call.is_static else ""
        out.append(f"{static}{call.ret_type} {call.callee}({sig});")
    return out


def _global_var_types(ir: FunctionIR) -> dict:
    """文件作用域变量名 → 类型拼写（由 C++ AST facts 提供）。"""
    return {
        str(item["name"]): str(item["type"])
        for item in ir.extensions.get("global_declarations", [])
        if item.get("name") and item.get("type")
    }


def _find_struct(ir: FunctionIR, name: str) -> list[dict] | None:
    for item in ir.extensions.get("record_types", []):
        if item.get("name") == name:
            return list(item.get("fields", []))
    return None


def _table_fixtures(ir, var_types) -> list:
    """指针表宏重定向：把 extern 的分发表/回调数组重定向到可写 fixture。"""
    lines = ["/* ---- 指针表 fixture（宏重定向到可写 fixture，stub 由 driver 安装） ---- */"]
    for call in ir.calls:
        if not call.ptr_call or not call.table_base:
            continue
        base = call.table_base
        t = (var_types.get(base) or "").replace("const", "") \
            .replace("*", "").replace("[]", "").strip()
        if not t:
            lines.append(f"/* {base}: 类型未知，未建 fixture */")
            continue
        if call.table_member:
            lines += [f"#undef {base}",
                      f"#define {base} ut_agent_fix_{base}",
                      f"static {t} ut_agent_fix_{base} = {{0}};"]
        else:
            lines += [f"#define UT_FIX_{base}_N 32   /* 回调数组长度未知，固定上限 */",
                      f"#undef {base}",
                      f"#define {base} ut_agent_fix_{base}",
                      f"static {t} ut_agent_fix_{base}[UT_FIX_{base}_N];"]
    return lines


def _strip_ptr(t: str) -> str:
    return t.replace("const", "").replace("*", "").replace("[]", "").strip()


def _ptr_depth(t: str) -> int:
    return t.replace(" ", "").count("*")


def _field_wiring(struct_decl: list[dict], prefix: str):
    """结构体指针字段接线：T* 字段 → 元素数组；T** 字段 → 两层（指针数组指向值数组）。
    返回 (声明行, designated 赋值列表)。"""
    decls, assigns = [], []
    for fld in struct_decl:
        ft = str(fld.get("type", ""))
        if "*" not in ft.replace(" ", ""):
            continue
        name = str(fld.get("name", ""))
        if not name:
            continue
        depth = _ptr_depth(ft)
        base = _strip_ptr(ft)
        if depth >= 2:
            decls.append(f"static {base} {prefix}_{name}_v[4] = {{0}};")
            decls.append(f"static {base} *{prefix}_{name}[4] = "
                         f"{{&{prefix}_{name}_v[0], &{prefix}_{name}_v[1], "
                         f"&{prefix}_{name}_v[2], &{prefix}_{name}_v[3]}};")
            assigns.append(f".{name} = {prefix}_{name}")
        else:
            decls.append(f"static {base} {prefix}_{name}[4] = {{0}};")
            assigns.append(f".{name} = {prefix}_{name}")
    return decls, assigns


def _config_fixture(ir, var_types, record_types) -> list:
    """配置表指针 fixture：宏重定向 + 结构体字段接线（含双重指针两层）。
    深层（字段结构的指针字段）不再展开，触及者执行期如实崩溃。"""
    if not ir.config_ptrs:
        return []
    lines = ["/* ---- 配置表 fixture（宏重定向 + 字段接线） ---- */"]
    init_lines = []
    for ptr in ir.config_ptrs:
        t = _strip_ptr(var_types.get(ptr) or "")
        if not t:
            lines.append(f"/* {ptr}: 类型未知，跳过 */")
            continue
        struct = record_types.get(t)
        decls, assigns = [], []
        if struct is not None:
            decls, assigns = _field_wiring(struct, f"ut_cfg_{ptr}")
        lines.append(f"#undef {ptr}")
        lines.append(f"#define {ptr} ut_ext_{ptr}")
        lines.append(f"static {t} *ut_ext_{ptr} = 0;   /* 指针变量 fixture */")
        lines += decls
        lines.append(f"static {t} ut_cfg_{ptr} = {{ {', '.join(assigns) or '0'} }};")
        init_lines.append(f"    ut_ext_{ptr} = &ut_cfg_{ptr};   /* 经宏重定向生效 */")
    if init_lines:
        lines.append("static void ut_agent_config_init(void)")
        lines.append("{")
        lines.extend(init_lines)
        lines.append("}")
    return lines


def _extern_fixtures(ir, src_path) -> list:
    """被测函数引用、但未在主文件定义的全局（extern 路由表等，定义在生成配置的 .c 里）
    → 宏重定向到零初始化 fixture（与指针表 fixture 同一手法）。"""
    src_name = Path(src_path).name
    declarations = {
        str(item["name"]): item
        for item in ir.extensions.get("global_declarations", [])
        if item.get("name")
    }
    defined = {
        name for name, item in declarations.items()
        if Path(str(item.get("file", src_name))).name == src_name
        and not item.get("is_extern", False)
    }
    externs = {
        name: item for name, item in declarations.items()
        if name not in defined
    }
    record_types = {
        str(item["name"]): list(item.get("fields", []))
        for item in ir.extensions.get("record_types", [])
        if item.get("name")
    }
    handled = {c.table_base for c in ir.calls if c.table_base} | set(ir.config_ptrs)
    lines = []
    for name in ir.globals_used:
        if name in defined or name in handled:
            continue
        decl = externs.get(name)
        if decl is None:
            continue
        t = str(decl.get("type", ""))
        if "[" in t:   # extern 数组（路由表）
            elem = _strip_ptr(t.split("[")[0])
            struct = record_types.get(elem)
            decls, assigns = [], []
            if struct is not None:
                decls, assigns = _field_wiring(struct, f"ut_ext_{name}_f")
            lines += [f"#undef {name}",
                      f"#define {name} ut_ext_{name}"]
            if decls:
                lines += decls
                lines.append(f"static {elem} ut_ext_{name}[4] = "
                             f"{{ {', '.join(assigns)}, {', '.join(assigns)} }};")
            else:
                lines.append(f"static {elem} ut_ext_{name}[4] = {{0}};")
        else:          # 标量/结构体全局
            elem = _strip_ptr(t)
            lines += [f"#undef {name}",
                      f"#define {name} ut_ext_{name}",
                      f"static {elem} ut_ext_{name} = {{0}};   /* extern 全局 fixture */"]
    if lines:
        lines.insert(0, "/* ---- extern 全局 fixture（定义在生成配置中的表/变量） ---- */")
    return lines


def build_harness_source(*args, call_max: int = 16) -> str:
    """Build a host harness from FunctionIR and C++-extracted source facts.

    The historical ``(tu, ir, driver_code)`` call shape is accepted for
    callers that have not migrated yet; ``tu`` is intentionally ignored and
    is no longer a Python translation-unit object.
    """
    if len(args) == 2:
        ir, driver_code = args
    elif len(args) == 3:
        _, ir, driver_code = args
    else:
        raise TypeError("build_harness_source expects (ir, driver_code)")
    if not isinstance(ir, FunctionIR):
        raise TypeError("build_harness_source requires a FunctionIR")
    src = Path(ir.file).read_text(encoding="utf-8", errors="replace").splitlines()
    first_def = _first_function_line(ir)
    # 头部窗口：保内容、补闭合（未闭合的 #if 追加 #endif，保住其中的宏定义；
    # 截断法会丢掉 PduRTpBuffer 这类跨函数的配置宏）
    window_head = _sanitize_pp(src[: first_def - 1])
    window_fn = _sanitize_pp(src[ir.line - 1: ir.line_end])  # 目标函数（孤儿指令清理）

    var_types = _global_var_types(ir)
    table_fix = _table_fixtures(ir, var_types)
    record_types = {
        str(item["name"]): list(item.get("fields", []))
        for item in ir.extensions.get("record_types", [])
        if item.get("name")
    }
    config_fix = _config_fixture(ir, var_types, record_types)
    extern_fix = _extern_fixtures(ir, ir.file)
    if config_fix:
        # 配置接线在 driver 主循环前执行一次
        driver_code = driver_code.replace(
            "    for (int i = 0", "    ut_agent_config_init();\n    for (int i = 0", 1)

    parts = [
        "/* ===== 自动抽取 TU（ut-agent M2.5）: 头部窗口 + fixture + 前置原型 + "
        "目标函数 + stub + driver ===== */",
        *window_head,
        "",
        *table_fix,
        *config_fix,
        *extern_fix,
        "",
        *_prototypes(ir),
        "",
        "/* ===== 目标函数 ===== */",
        *window_fn,
        "",
        "/* ===== stub（自动生成） ===== */",
        render_spec_stub_c(ir, call_max, with_prelude=False),
        "",
        driver_code,
    ]
    return "\n".join(parts) + "\n"
