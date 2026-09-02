"""M2：WinAMS stub 源码生成（确定性）。

主入口 ``render_stub_c`` 直接生成参考工程使用的
``AMSTB_ / CALLCNT_ / ARG / PTROUT / AMIN_return`` 契约。旧 host
回放器仍通过 ``render_spec_stub_c`` 使用其内部 fixture 格式，不属于
WinAMS 交付物。
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ut_agent.ir import FunctionIR, Param, TypeInfo

CALL_MAX_DEFAULT = 16


def _pointee_type(param: Param) -> str:
    """Read the extractor-owned pointee spelling for generated C declarations."""
    if param.type_info is None or not param.type_info.pointee_type:
        raise ValueError(f"形参 {param.name} 缺少 typed pointee fact")
    return param.type_info.pointee_type.replace("const ", "").strip()


def _is_scalar(type_info: TypeInfo | None) -> bool:
    return bool(type_info and type_info.is_scalar)


def _pointee_is_scalar(type_info: TypeInfo | None) -> bool:
    return bool(
        type_info
        and type_info.kind == "pointer"
        and type_info.pointee_info
        and type_info.pointee_info.is_scalar
    )


def table_stub_name(call) -> str:
    """指针表安装 stub 的函数名（分发表成员 / 回调数组）。"""
    k = f"{call.order:02d}"
    if call.table_member:
        return f"stub{k}_{call.table_base}_{call.table_member}"
    return f"stub{k}_{call.table_base}"


def render_spec_stub_c(ir: FunctionIR, call_max: int = CALL_MAX_DEFAULT,
                       with_prelude: bool = True) -> str:
    """with_prelude=False：嵌入 host 执行器单 TU 用（不带文件头注释与 #include）。"""
    if call_max <= 0:
        raise ValueError("call_max 必须大于 0")
    out: list[str] = []
    if with_prelude:
        out += [
            "/*",
            f" * 自动生成 stub —— 被测函数: {ir.name}",
            f" * 源码: {ir.file} L{ir.line}",
            " *",
            " * 规则: 被测函数内所有调用函数 stub 化; 只留 callcnt 与引数入出力; 无逻辑",
            " * 命名: stub 编号 k 按调用顺序从 00 起; ARG<k>_<形参名>=入力记录;",
            " *       PTIN<k>_<形参名>=只读/输入指针记录; PTOUT<k>_<形参名>[CALL_MAX]=传出设定;",
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
                if i < len(call.arg_type_infos) and _is_scalar(call.arg_type_infos[i]):
                    out.append(f"{t} ARG{k}_arg{i}[CALL_MAX];  /* 入力记录 */")
            out.append(f"static void {name}({sig})   /* 安装至 {call.table_base}"
                       f"{'.' + call.table_member if call.table_member else '[]'} */")
            out.append("{")
            out.append(f"    if (callcnt{k} >= CALL_MAX) return;")
            for i, t in enumerate(call.arg_types):
                if i < len(call.arg_type_infos) and _is_scalar(call.arg_type_infos[i]):
                    out.append(f"    ARG{k}_arg{i}[callcnt{k}] = arg{i};")
            out.append(f"    callcnt{k}++;   /* 先记录后递增, 索引从 0 起 */")
            out.append("}")
            out.append("")
            continue
        out.append(f"uint32 callcnt{k} = 0;   /* 每用例执行前置 0 */")
        ptin_scalar, ptout_scalar = {}, {}
        for p in call.params:
            if p.is_ptr:
                elem = _pointee_type(p)
                if not _pointee_is_scalar(p.type_info):
                    out.append(f"/* {p.name}: 指向物为结构体（含 const 成员），v0 不记录/不设定 */")
                    continue
                if p.is_const or not _call_param_is_written(p):
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
        out.append(f"    if (callcnt{k} >= CALL_MAX) {{")
        out.append("        return;" if not has_ret else "        return 0;")
        out.append("    }")
        for p in call.params:
            if not p.is_ptr:
                out.append(f"    ARG{k}_{p.name}[callcnt{k}] = {p.name};")
            elif (p.is_const or not _call_param_is_written(p)) and p.name in ptin_scalar:
                out.append(f"    PTIN{k}_{p.name}[callcnt{k}] = *{p.name};")
        if ptout_scalar:
            out.append(f"    if (callcnt{k} < CALL_MAX) {{")
            for p in call.params:
                if _call_param_is_written(p) and p.name in ptout_scalar:
                    out.append(f"        *{p.name} = PTOUT{k}_{p.name}[callcnt{k}];")
            out.append("    }")
        out.append(f"    callcnt{k}++;   /* 先记录后递增, 索引从 0 起 */")
        if has_ret:
            if ret_controls:
                out.append(f"    return CALLRET{k}[callcnt{k} - 1];")
            else:
                out.append("    return 0;   /* 返回值未参与分支判定: 不加 CALLRET, 返回类型零值 */")
        out.append("}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# WinAMS の STB_ARYSIZE と同じ扱いにする。プロジェクトごとに CLI の
# --call-max で上書きできる（参考プロジェクトの AMSTB_SrcFile.c は 5）。
WINAMS_CALL_MAX_DEFAULT = 5


def _winams_ident(name: str) -> str:
    """C 識別子として使えない関数ポインタ表記を安定した名前にする。"""
    out = []
    for ch in name:
        out.append(ch if ch.isalnum() or ch == "_" else "_")
    ident = "".join(out).strip("_")
    return ident or "anonymous"


def _ordered_stub_calls(ir: FunctionIR) -> list:
    """Return one native stub definition per callee, in first-call order."""
    calls = []
    seen: set[str] = set()
    for call in sorted(ir.calls, key=lambda item: item.order):
        callee = (call.callee or "").strip()
        if not callee or callee in seen:
            continue
        seen.add(callee)
        calls.append(call)
    return calls


def _winams_is_scalar(type_info: TypeInfo | None) -> bool:
    return _pointee_is_scalar(type_info)


def _call_param_is_written(param) -> bool:
    """Return whether a call parameter is proven to be written by its callee.

    Declarations without a visible definition remain conservative for backward
    compatibility: a non-const pointer may still be an output.  Once the
    parser has a definition, ``write_status=known`` makes a read-only pointer
    input-only and prevents the stub from fabricating a write-back.
    """
    if param.write_status == "known":
        return param.is_written
    return param.is_written


def render_stub_c(ir: FunctionIR, call_max: int = WINAMS_CALL_MAX_DEFAULT,
                  with_prelude: bool = True,
                  extra_includes: Sequence[str] = ()) -> str:
    """WinAMS 原生 stub 源码。

    参考项目的命名规则是：调用仍使用原函数名，由 WinAMS 的
    ``STB_PREFIX=AMSTB_`` 接管到 ``AMSTB_<callee>``；调用履历使用
    ``CALLCNT_<callee>``，普通参数使用 ``ARG<argno>_<callee>``，可写
    指针输出使用 ``PTROUT<argno>_<callee>``，返回值使用
    ``AMIN_return``。这与项目原先的 ``callcnt00`` 格式不同，因此该
    函数作为主生成入口直接输出 WinAMS 格式。
    """
    if call_max <= 0:
        raise ValueError("call_max 必须大于 0")

    out: list[str] = []
    if with_prelude:
        out += [
            "#define WINAMS_STUB",
            "#ifdef WINAMS_STUB",
            "#ifdef __cplusplus",
            'extern "C" {',
            "#endif",
            "",
            f"#define CALL_MAX  {call_max}",
            "",
            '/* WinAMS 参考工程的公共类型头；项目头路径由编译命令提供。 */',
            '#include "aipf_std_def.h"',
            '#include "Platform_Types.h"',
            '#include "Std_Types.h"',
            '#include "Compiler.h"',
        ]
        seen = {"aipf_std_def.h", "Platform_Types.h", "Std_Types.h", "Compiler.h"}
        for header in extra_includes:
            if header and header not in seen:
                out.append(f'#include "{header}"')
                seen.add(header)
        out.append("")
    else:
        out.append(f"#define CALL_MAX  {call_max}")
        out.append("")

    declarations: list[str] = []
    bodies: list[str] = []
    for call in _ordered_stub_calls(ir):
        callee = _winams_ident(call.callee)
        stub_name = f"AMSTB_{callee}"
        params_sig = ", ".join(f"{p.type} {p.name}" for p in call.params)
        if not params_sig:
            params_sig = " void "
        declarations.append(
            f"{call.ret_type} {stub_name}({params_sig}) __attribute__((used));"
        )

        body: list[str] = [
            f"/* WINAMS_STUB[{Path(ir.file).name}:{call.callee}:{stub_name}:"
            f"inout:::counter<CALLCNT_{callee}>] */",
            f"/*    {call.callee} => Stub */",
            f"{call.ret_type} {stub_name}({params_sig})",
            "{",
            f"    static volatile u1 CALLCNT_{callee};",
        ]

        for idx, param in enumerate(call.params):
            slot = f"{idx:02d}"
            if not param.is_ptr:
                body.append(
                    f"    static volatile {param.type} ARG{slot}_{callee}[ CALL_MAX ];"
                )
                continue
            base = _pointee_type(param)
            if _call_param_is_written(param) and _winams_is_scalar(param.type_info):
                body.append(
                    f"    static volatile {base} PTROUT{slot}_{callee}[ CALL_MAX ];"
                )
            else:
                body.append(
                    f"    static {base}* volatile PTROUT{slot}_{callee}[ CALL_MAX ];"
                )

        has_return = call.ret_type not in ("", "void")
        if has_return:
            body.append(f"    static volatile {call.ret_type} AMIN_return[CALL_MAX];")
        body.append("")
        body.append(f"    CALLCNT_{callee}++;" )

        for idx, param in enumerate(call.params):
            slot = f"{idx:02d}"
            index = f"CALLCNT_{callee} - 1"
            if not param.is_ptr:
                body.append(
                    f"    ARG{slot}_{callee}[{index}] = {param.name};"
                )
            elif _call_param_is_written(param) and _winams_is_scalar(param.type_info):
                body += [
                    "",
                    "    /* WinAMS 参考 stub：按调用序设定传出值 */",
                    f"    *{param.name} = PTROUT{slot}_{callee}[{index}];",
                ]
            else:
                body += [
                    "",
                    "    /* 指针地址记录，供 WinAMS 的 @地址 列使用 */",
                    f"    PTROUT{slot}_{callee}[{index}] = {param.name};",
                ]

        if has_return:
            body += ["", f"    return AMIN_return[CALLCNT_{callee} - 1];"]
        body += ["}", ""]
        bodies.extend(body)

    out += declarations
    out.append("/*--------------------------------- stub function --------------------------------*/")
    out.extend(bodies)
    # ARM GCC 链接阶段仍需要解析被测 C 文件中的原调用名。WinAMS 运行时
    # 通过 STB_PREFIX 使用 AMSTB_*，而 GNU ld 不会自动建立这个映射；同一
    # TU 中的别名只补齐 ELF 符号，不改变 TestCsv 的 AMSTB 契约。
    aliases: list[str] = []
    for call in _ordered_stub_calls(ir):
        if call.ptr_call:
            continue
        ident = _winams_ident(call.callee)
        params_sig = ", ".join(f"{p.type} {p.name}" for p in call.params) or " void "
        aliases.append(
            f"{call.ret_type} {call.callee}({params_sig}) "
            f"__attribute__((alias(\"AMSTB_{ident}\"), used));"
        )
    if aliases:
        out.append("/* ARM GCC link aliases: original call -> AMSTB symbol */")
        out.extend(aliases)
    if with_prelude:
        out += [
            "#ifdef __cplusplus",
            "}",
            "#endif",
            "#endif /* WINAMS_STUB */",
        ]
    return "\n".join(out).rstrip() + "\n"
