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
    """Return the target declaration line from the typed FunctionIR."""
    return max(1, int(ir.line))


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


def _table_fixtures(ir) -> list:
    """Do not invent declarations absent from the typed extractor contract."""
    return []


def _config_fixture(ir) -> list:
    """Configuration fixtures require an explicit typed declaration pass."""
    return []


def _extern_fixtures(ir, src_path) -> list:
    """被测函数引用、但未在主文件定义的全局（extern 路由表等，定义在生成配置的 .c 里）
    → 宏重定向到零初始化 fixture（与指针表 fixture 同一手法）。"""
    # The v3 contract deliberately has no untyped declaration bag.  Do not
    # synthesize C declarations from source spelling or legacy metadata.
    return []


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

    table_fix = _table_fixtures(ir)
    config_fix = _config_fixture(ir)
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
