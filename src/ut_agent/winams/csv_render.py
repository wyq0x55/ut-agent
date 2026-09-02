"""WinAMS TestCsv 渲染（确定性）。

主入口 ``render_csv`` 输出 WinAMS 的 ``mod/#COMMENT/;$L$`` 格式；
旧 host 回放器只通过 ``render_spec_csv`` 使用内部文本格式。
"""
from __future__ import annotations

import csv
import io
from dataclasses import asdict
from itertools import product
import re
from pathlib import Path

from ut_agent.cases import boundary
from ut_agent.ir import FunctionIR
from ut_agent.rules.model import GenerationResult, TestIntent
from ut_agent.rules.engine import evaluate_branch
from ut_agent.winams.projection import WinAMSProjection


_WINAMS_RTE_PORT_ORDER = (
    "dlsw", "slsw", "npc", "ig", "pulse", "haf", "ful", "ntrl",
    "clsw", "main", "pwl", "vipsw", "fosw", "ohsw", "ihopsw",
    "ihclsw", "otopsw", "otclsw", "otsw", "plopsw", "plclsw", "rrsw",
)
_WINAMS_RTE_PORT_RANK = {
    name: index for index, name in enumerate(_WINAMS_RTE_PORT_ORDER)
}
_WINAMS_RTE_PORT = re.compile(
    r"_(" + "|".join(_WINAMS_RTE_PORT_ORDER) + r")(?:_|$)"
)
_DEAD_BRANCH_COMMENT = "デッドコードがあった為、この分岐に入ることができません"


def _skip_memory_helpers(ir: FunctionIR, call) -> bool:
    """寄存器 helper 是实现细节，不生成重复的 stub 设定列。

    即使当前函数因已有返回值/指针结果而没有选中写寄存器，helper 调用
    本身也不能表示新的测试分歧，因此仍然跳过。
    """
    return call.callee_kind == "memory_helper"


def _is_scalar(type_info) -> bool:
    return bool(type_info and type_info.is_scalar)


def _global_write_names(ir: FunctionIR) -> list[str]:
    """Projection policy: use extractor-proven global write facts verbatim."""
    return list(ir.global_writes)


def _global_records(ir: FunctionIR) -> list[dict]:
    return [asdict(item) for item in ir.global_objects]


def build_columns(ir: FunctionIR, cand: dict) -> list:
    """列模型：[{"header", "kind"(set/expect/record), "values", "enum", "cv"}] 按规格顺序。
    指针引数：未写回的 T*（传入）→ 指向物设定列；实际写回的 T*（传出）→ <名>_out 期待列，
    并登记 @地址 行（规格 §4.1：@引数名 分配地址，其前地址段空闲可用）。"""
    cols: list = []
    for call in ir.calls:
        if _skip_memory_helpers(ir, call):
            continue
        k = f"{call.order:02d}"
        if call.ptr_call:
            cols.append({"header": f"callcnt{k}(期待·指针表)", "kind": "expect", "values": None})
            for i, t in enumerate(call.arg_types):
                if i < len(call.arg_type_infos) and _is_scalar(call.arg_type_infos[i]):
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
        if p.is_ptr and p.type_info is not None and p.type_info.pointer_depth < 2:
            if p.is_written:
                cols.append({"header": f"{p.name}_out(期待)", "kind": "expect", "values": None,
                             "cv": None, "ptr_param": p})
            else:
                cols.append({"header": f"{p.name}(设定)", "kind": "set", "values": [0],
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
    for w in _global_write_names(ir):
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
    is_bool = bool(cv and cv.type_info and cv.type_info.kind == "bool")
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


def render_spec_csv(ir: FunctionIR, cfg_display: str) -> str:
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


# ---------------------------------------------------------------------------
# WinAMS 原生 TestCsv 格式

def _winams_quote(value: object) -> str:
    """WinAMS CSV 的字符串字段一律加双引号。"""
    text = str(value).replace('"', '""')
    return f'"{text}"'


def _winams_condition(branch) -> str:
    # TestCsv conditions must describe the expression WinAMS evaluates.  Keep
    # the source spelling in FunctionIR for provenance, but prefer Clang's
    # semantic pretty-print when the standalone extractor supplied it.  The
    # legacy parser leaves cond_text_expanded empty and therefore keeps its
    # existing behaviour.
    condition = branch.cond_text_expanded or branch.cond_text
    if condition:
        try:
            condition.encode("cp932")
        except UnicodeEncodeError:
            # FunctionIR JSON is UTF-8, while WinAMS TestCsv is CP932.  A
            # legacy source comment can contain bytes which Clang had to
            # repair to U+FFFD; keep the semantic expanded condition instead
            # of making the whole candidate CSV unencodable.
            if branch.cond_text_expanded:
                return branch.cond_text_expanded
        if branch.kind == "switch" and not condition.lstrip().startswith("switch"):
            return f"switch ( {condition.strip()} )"
        keyword = {
            "if": "if",
            "elseif": "else if",
            "while": "while",
            "dowhile": "do while",
            "for": "for",
        }.get(branch.kind)
        if keyword and not condition.lstrip().startswith(keyword):
            return f"{keyword} ( {condition.strip()} )"
        return condition
    if branch.kind == "switch":
        return "switch"
    return "(condition unavailable)"


def _winams_comment_source(call) -> str:
    callee = call.callee
    return f"AMSTB_SrcFile.c/AMSTB_{callee}@CALLCNT_{callee}"


def _winams_stub_declarations(ir: FunctionIR) -> list[tuple[str, str, str]]:
    """Return native TestCsv ``%`` declarations for the calls in the IR."""
    declarations: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    source_file = Path(ir.file).name
    calls = _winams_stub_calls(ir)
    # The declaration block is a WinAMS stub registry, not the IO-column
    # order.  Same-source static helpers are emitted first; external helpers
    # are then emitted by stub name.  The IO columns below still use first
    # call-site order, as required by the testcase contract.
    calls = sorted(
        calls,
        key=lambda call: (
            not bool(call.is_static),
            str(call.callee),
        ),
    )
    for call in calls:
        callee = (call.callee or "").strip()
        if not callee or callee in seen:
            continue
        seen.add(callee)
        # WinAMS qualifies a same-source ``static`` helper with the tested
        # source file.  External helpers keep the short declaration name.
        # This is the distinction visible in the original ``%`` rows:
        # ``p_blm.c/p_u1l_...`` versus ``p_u1g_...``.
        target = f"{source_file}/{callee}" if call.is_static else callee
        declarations.append(("%", f"AMSTB_{callee}", target))
    return declarations


def _winams_stub_calls(ir: FunctionIR) -> list:
    """Return non-memory-helper calls, deduplicated by stub callee."""
    calls = []
    seen: set[str] = set()
    for call in sorted(ir.calls, key=lambda item: item.order):
        callee = (call.callee or "").strip()
        if not callee or callee in seen or _skip_memory_helpers(ir, call):
            continue
        seen.add(callee)
        calls.append(call)

    # CALLCNT follows source order, with the rear-seat open/close pair using
    # the generated Rte registration order.
    index = 0
    while index < len(calls):
        if _winams_port_group(calls[index]) not in {"plclsw", "plopsw"}:
            index += 1
            continue
        end = index
        while end < len(calls) and _winams_port_group(calls[end]) in {
            "plclsw", "plopsw"
        }:
            end += 1
        run = calls[index:end]
        ports = {_winams_port_group(call) for call in run}
        if {"plclsw", "plopsw"}.issubset(ports):
            calls[index:end] = sorted(
                run,
                key=lambda call: (
                    0 if _winams_port_group(call) == "plopsw" else 1,
                    call.order,
                ),
            )
        index = end
    return calls


def _winams_port_group(call) -> str | None:
    """Return the known Rte switch-port token carried by a call."""
    callee = str(getattr(call, "callee", "") or "")
    if not callee.startswith("Rte_Write_"):
        return None
    match = _WINAMS_RTE_PORT.search(callee)
    return match.group(1) if match else None


def _winams_branch_calls(ir: FunctionIR) -> list:
    """Return stub calls in WinAMS's branch-IO port order."""
    calls = _winams_stub_calls(ir)
    index = 0
    while index < len(calls):
        if _winams_port_group(calls[index]) is None:
            index += 1
            continue
        end = index
        while end < len(calls) and _winams_port_group(calls[end]) is not None:
            end += 1
        run = calls[index:end]
        ports = {_winams_port_group(call) for call in run}
        if len(ports) > 1:
            calls[index:end] = sorted(
                run,
                key=lambda call: (
                    _WINAMS_RTE_PORT_RANK[_winams_port_group(call)],
                    call.order,
                ),
            )
        index = end
    return calls


def _winams_branch_call_lines(calls: list) -> dict[int, int]:
    """Map reordered port calls back onto their source-event line slots."""
    lines: dict[int, int] = {}
    index = 0
    while index < len(calls):
        if _winams_port_group(calls[index]) is None:
            index += 1
            continue
        end = index
        while end < len(calls) and _winams_port_group(calls[end]) is not None:
            end += 1
        source_slots = sorted(call.line for call in calls[index:end])
        for slot, call in zip(source_slots, calls[index:end]):
            lines[call.order] = slot
        index = end
    return lines


def _provenance_line(value: object, fallback: int) -> int:
    """Return the first source line carried by an IR fact."""
    provenance = getattr(value, "provenance", None)
    if provenance is None and isinstance(value, dict):
        provenance = value.get("provenance")
    spelling = getattr(provenance, "spelling", None)
    if spelling is None and isinstance(provenance, dict):
        spelling = provenance.get("spelling")
    line = getattr(spelling, "line", None)
    if line is None and isinstance(spelling, dict):
        line = spelling.get("line")
    try:
        return int(line) if line is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _append_column(
    columns: list[tuple[str, str | None]],
    seen: set[str],
    column: tuple[str, str | None],
) -> None:
    """Append one column once, retaining the first deterministic occurrence."""
    if column[0] in seen:
        return
    seen.add(column[0])
    columns.append(column)


def _is_local_fact(name: str, ir: FunctionIR) -> bool:
    """Do not expose automatic function locals as WinAMS IO columns."""
    compact = str(name).replace(" ", "")
    base = compact.split(".", 1)[0].split("[", 1)[0]
    return compact in ir.locals or base in ir.locals


def _winams_static_function(ir: FunctionIR) -> bool:
    """Return whether WinAMS exposes scalar parameters through an address alias.

    The legacy WinAMS projects use ``@param`` for parameters of file-static
    test targets.  This is a target-storage convention, not a generic scalar
    type rule, so it must come from the parser rather than from a CSV-shaped
    heuristic.
    """
    return bool(ir.is_static)


def _winams_function_return_comment(ir: FunctionIR) -> str:
    """Return the WinAMS comment name for the tested function return.

    WinAMS qualifies a file-static target with its source file, while an
    externally visible target is referenced by its function name alone.  The
    distinction is part of the source symbol contract and must not be inferred
    from the reference TestCsv.
    """
    if _winams_static_function(ir):
        return f"{Path(ir.file).name}/{ir.name}@@"
    return f"{ir.name}@@"


def _winams_param_access_paths(param, *, writes_only: bool = False) -> list[str]:
    """Return deterministic AST lvalue paths carried by a parameter."""
    raw = param.access_paths
    if not isinstance(raw, (list, tuple)):
        return []
    entries: list[tuple[int, int, str]] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            path, read, write, offset = item, True, False, index
        elif isinstance(item, dict):
            path = str(item.get("path", ""))
            read = bool(item.get("read", False))
            write = bool(item.get("write", False))
            try:
                offset = int(item.get("offset", index))
            except (TypeError, ValueError):
                offset = index
        else:
            continue
        if not path or (writes_only and not write):
            continue
        entries.append((offset, index, path))
    return [path for _, _, path in sorted(entries)]


def _winams_param_write_columns(param, *, static_function: bool = False) -> list[str]:
    """Map AST pointer write paths to WinAMS output column spelling."""
    paths = _winams_param_access_paths(param, writes_only=True)
    name = str(param.name)
    columns: list[str] = []
    for path in paths:
        if path == name:
            # Assignment to the pointer variable itself changes only the local
            # pointer value; it is not a write to the caller-visible object.
            continue
        if path.startswith("*"):
            if static_function and path == f"*{name}":
                column = f"@{name}[0]"
            else:
                column = path
        elif path.startswith(name):
            column = f"@{path}"
        else:
            column = f"*{name}"
        if column not in columns:
            columns.append(column)
    if not columns and getattr(param, "is_written", False):
        columns.append(f"*{name}")
    return columns


def _winams_param_read_columns(param, *, static_function: bool = False) -> list[str]:
    """Map dereferenced parameter reads to settable WinAMS input columns."""
    paths = _winams_param_access_paths(param)
    name = str(param.name)
    columns: list[str] = []
    for path in paths:
        if path == name:
            continue
        if static_function and path == f"*{name}":
            column = f"@{name}[0]"
        elif static_function and path.startswith(name):
            column = f"@{path}"
        else:
            column = path
        if column not in columns:
            columns.append(column)
    return columns


def _winams_call_capacity(call) -> int:
    """Return the statically derivable number of slots for one call site.

    The extractor records loop trip counts in the formal ``max_occurrences``
    field.  An IR without that fact remains one source slot; this keeps the
    renderer deterministic without consulting a reference TestCsv.
    """
    try:
        return max(1, int(call.max_occurrences))
    except (TypeError, ValueError):
        return 1


def _winams_call_occurrences(ir: FunctionIR, callee: str) -> list:
    return [
        call for call in sorted(ir.calls, key=lambda item: item.order)
        if (call.callee or "").strip() == callee
        and not _skip_memory_helpers(ir, call)
    ]


def _winams_stub_capacity(ir: FunctionIR, call) -> int:
    """Sum source occurrences and statically known loop capacities."""
    # A macro-generated wrapper (notably the Rte write wrappers) is one
    # WinAMS-visible argument slot even when the expanded wrapper occurs at
    # several call sites.  The call counter still records every invocation;
    # only the visible ARG/PTROUT sample remains single-slot.
    if getattr(call, "via_macro", None):
        return 1
    occurrences = _winams_call_occurrences(ir, call.callee)
    return max(1, sum(_winams_call_capacity(item) for item in occurrences))


def _winams_return_used(call) -> bool:
    """Whether a non-void stub return is an observable input.

    The Clang extractor writes this fact explicitly.  Treat absent metadata as
    used for compatibility with older FunctionIR documents.
    """
    return bool(call.return_used)


def _winams_param_fields(call, index: int) -> list[str]:
    """Read optional Clang-provided fields observed through a pointer param."""
    # The caller-side observation is narrower and takes precedence over the
    # complete callee struct shape discovered from a context source.
    metadata = call.caller_param_fields or call.param_fields
    raw = None
    if isinstance(metadata, dict):
        raw = metadata.get(str(index), metadata.get(index))
    elif isinstance(metadata, list):
        if metadata and all(isinstance(item, str) for item in metadata):
            raw = metadata if index == 0 else None
        elif index < len(metadata):
            raw = metadata[index]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).lstrip(".") for item in raw if str(item).lstrip(".")]


def _winams_pointer_argument_info(call, index: int) -> dict:
    raw = call.pointer_arguments
    if not isinstance(raw, dict):
        return {}
    value = raw.get(str(index), raw.get(index, {}))
    return value if isinstance(value, dict) else {}


def _winams_stub_pointee_columns(
    ir: FunctionIR, call, *, index: int | None = None
) -> list[str]:
    """Expand address-passed PAL setter data as a stub array element."""
    callee = str(call.callee or "")
    if not (callee.startswith("pal_") and "_set_" in callee):
        return []
    capacity = _winams_stub_capacity(ir, call)
    columns: list[str] = []
    for param_index, param in enumerate(call.params):
        if index is not None and param_index != index:
            continue
        if not param.is_ptr:
            continue
        info = _winams_pointer_argument_info(call, param_index)
        if not info.get("is_address") or info.get("is_null"):
            continue
        slot_name = f"PTROUT{param_index:02d}_{call.callee}"
        columns.extend(
            f"{slot_name}[{slot}][0]" for slot in range(capacity)
        )
    return columns


def _winams_stub_return_first(call) -> bool:
    """Return whether this API's WinAMS slot precedes its pointer input."""
    callee = str(call.callee or "")
    return callee.startswith("pal_") and "_get_" in callee


def _winams_return_fields(call) -> list[str]:
    raw = call.return_fields
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).lstrip(".") for item in raw if str(item).lstrip(".")]


def _winams_stub_param_columns(ir: FunctionIR, call) -> list[str]:
    """Expand stub arguments by parameter, then by deterministic call slot."""
    capacity = _winams_stub_capacity(ir, call)
    columns: list[str] = []
    for index, param in enumerate(call.params):
        slot_name = f"PTROUT{index:02d}_{call.callee}" if param.is_ptr \
            else f"ARG{index:02d}_{call.callee}"
        fields = _winams_param_fields(call, index) if param.is_ptr else []
        if fields:
            # WinAMS expands a structure-valued stub parameter field-major:
            # all call slots for field 0, then all slots for field 1.
            for field in fields:
                columns.extend(
                    f"{slot_name}[{slot}].{field}"
                    for slot in range(capacity)
                )
        else:
            columns.extend(
                f"{slot_name}[{slot}]" for slot in range(capacity)
            )
        columns.extend(_winams_stub_pointee_columns(ir, call, index=index))
    return columns


def _winams_stub_param_output_columns(ir: FunctionIR, call) -> list[str]:
    """Expand only caller-observable stub argument write-back fields."""
    callee = call.callee or ""
    raw_observable = call.caller_param_output
    if not isinstance(raw_observable, dict):
        if not callee.startswith("Rte_Read_") and not (
            callee.startswith("pal_") and "_get_" in callee
        ):
            pointee_columns = _winams_stub_pointee_columns(ir, call)
            if pointee_columns:
                return pointee_columns
            return _winams_stub_param_columns(ir, call)
        raw_observable = {}

    capacity = _winams_stub_capacity(ir, call)
    columns: list[str] = []
    for index, param in enumerate(call.params):
        slot_name = f"PTROUT{index:02d}_{call.callee}" if param.is_ptr \
            else f"ARG{index:02d}_{call.callee}"
        # Rte_Read APIs fill a temporary/local receive object.  WinAMS still
        # exposes the PTROUT slot as a stub input, but the local receive value
        # is not an output of the tested function.  Keep this semantic rule
        # independent of whether the extractor had enough AST information to
        # attach caller_param_output metadata.
        if param.is_ptr and (
            callee.startswith("Rte_Read_")
            or (callee.startswith("pal_") and "_get_" in callee)
        ):
            continue
        raw_value = raw_observable.get(
            str(index), raw_observable.get(index, True)
        )
        if param.is_ptr and not bool(raw_value):
            continue
        pointee_columns = _winams_stub_pointee_columns(
            ir, call, index=index
        )
        if pointee_columns and param.is_ptr:
            columns.extend(pointee_columns)
            continue
        fields = _winams_param_fields(call, index) if param.is_ptr else []
        if fields:
            for field in fields:
                columns.extend(
                    f"{slot_name}[{slot}].{field}"
                    for slot in range(capacity)
                )
        else:
            columns.extend(
                f"{slot_name}[{slot}]" for slot in range(capacity)
            )
    return columns


def _winams_stub_return_columns(ir: FunctionIR, call) -> list[str]:
    if not _winams_return_used(call):
        return []
    capacity = _winams_stub_capacity(ir, call)
    fields = _winams_return_fields(call)
    columns: list[str] = []
    if fields:
        # WinAMS expands a structure-valued return field-major: all call
        # slots for one field precede the next field.  This is the same
        # layout used for structure-valued pointer parameters.
        for field in fields:
            columns.extend(
                f"AMIN_return[{slot}].{field}"
                for slot in range(capacity)
            )
    else:
        columns.extend(f"AMIN_return[{slot}]" for slot in range(capacity))
    return columns


def _winams_global_columns(
    ir: FunctionIR, obj: dict, *, direction: str = ""
) -> list[str]:
    """Expand one Clang global fact using WinAMS source/field spelling."""
    name = str(obj["name"])
    is_const = bool(obj.get("is_const", False))
    is_volatile = bool(obj.get("is_volatile", False))
    # Read-only configuration is resolved by the compile context, not exposed
    # as a settable testcase variable.  Volatile const calibration values are
    # intentionally retained under their original short name (WinAMS treats
    # them as calibration symbols rather than ordinary source globals).
    if is_const and not is_volatile:
        return []
    if is_const and is_volatile:
        prefix = ""
    else:
        source_file = str(obj.get("source_file") or Path(ir.file).name)
        # Header declarations are visible to WinAMS by their symbol name;
        # only a definition in a C translation unit receives the source-file
        # qualification.  Clang keeps the declaration file as provenance,
        # so applying the same prefix rule to ``*.h`` incorrectly exposes
        # names such as ``eAPL_EXP.h/u1g_x``.
        suffix = Path(source_file).suffix.lower()
        prefix = "" if suffix in {".h", ".hh", ".hpp", ".hxx"} \
            else f"{Path(source_file).name}/"
    base = f"{prefix}{name}"
    sizes: list[int] = []
    for raw_size in obj.get("array_sizes", ()):
        try:
            sizes.append(max(0, int(raw_size)))
        except (TypeError, ValueError):
            return [base]
    field_paths = [
        str(item).lstrip(".")
        for item in obj.get("field_paths", ())
        if str(item).lstrip(".")
    ]
    field_accesses = {}
    field_access_order: dict[str, tuple[int, int, int]] = {}
    raw_accesses = obj.get("field_accesses", ())
    if isinstance(raw_accesses, (list, tuple)):
        for item in raw_accesses:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).lstrip(".")
            if not path:
                continue
            field_accesses[path] = (
                bool(item.get("read", False)),
                bool(item.get("write", False)),
            )
            try:
                line = int(item.get("line", 0))
            except (TypeError, ValueError):
                line = 0
            try:
                offset = int(item.get("offset", 0))
            except (TypeError, ValueError):
                offset = 0
            field_access_order[path] = (line, offset, len(field_access_order))
    if field_paths and field_accesses:
        def has_access(path: str, direction_name: str) -> bool:
            """Match a field or descendant for ordinary record objects."""
            for access, (read, write) in field_accesses.items():
                if path != access and not path.startswith(access + "."):
                    continue
                if direction_name == "read" and read:
                    return True
                if direction_name == "write" and write:
                    return True
            return False

        def has_exact_access(path: str, direction_name: str) -> bool:
            read, write = field_accesses.get(path, (False, False))
            return read if direction_name == "read" else write

        def has_write_aggregate(path: str) -> bool:
            """Select descendants only when the union member was written."""
            return any(
                write and (path == access or path.startswith(access + "."))
                for access, (_, write) in field_accesses.items()
            )

        def has_copied_access(path: str) -> bool:
            """Identify fields copied from an observed local record."""
            if not isinstance(raw_accesses, (list, tuple)):
                return False
            return any(
                isinstance(item, dict)
                and bool(item.get("copied_from_local", False))
                and (path == str(item.get("path", "")).lstrip(".")
                     or path.startswith(
                         str(item.get("path", "")).lstrip(".") + "."
                     ))
                for item in raw_accesses
            )

        def select_union_field(path: str) -> bool:
            # A union's read aggregate can be an alias caused by a member
            # read.  Expanding that alias would expose every alternative and
            # is the source of the old over-expanded TestCsv columns.  A
            # written aggregate, however, means the whole selected member is
            # initialized/observed and its leaves are required.
            exact = has_exact_access(path, "read") or has_exact_access(
                path, "write"
            )
            if direction == "input" and not obj.get("write"):
                return exact
            return exact or has_write_aggregate(path)

        if obj.get("is_union"):
            original_order = {
                path: index for index, path in enumerate(field_paths)
            }
            field_paths = [path for path in field_paths
                           if select_union_field(path)]
            if direction in {"input", "output"}:
                # Keep fields belonging to a written union aggregate in
                # declaration order.  Exact read-only alternatives are
                # ordered by their AST access event; this preserves the
                # source order for initialized members while retaining the
                # actual read order for aliases selected individually.
                field_paths.sort(key=lambda path: (
                    0 if has_write_aggregate(path) else 1,
                    field_access_order.get(
                        path, (2**31 - 1, 2**63 - 1,
                               original_order.get(path, len(original_order)))
                    )
                    if has_write_aggregate(path) and has_copied_access(path)
                    else original_order.get(path, len(original_order))
                    if has_write_aggregate(path)
                    else field_access_order.get(
                        path, (2**31 - 1, 2**63 - 1,
                               original_order.get(path, len(original_order)))
                    ),
                ))
        elif direction in {"input", "output"}:
            # WinAMS initializes every non-union field touched by the target,
            # including fields written before a later read.
            field_paths = [path for path in field_paths
                           if has_access(path, "read")
                           or has_access(path, "write")]
            if obj.get("write") and not obj.get("read"):
                # For a write-only aggregate, WinAMS follows the target's
                # first write event rather than the record declaration order.
                # This matters when initialization writes a later-declared
                # field before an earlier-declared field.  Preserve the
                # declaration order only for fields without a direct AST
                # access location.
                original_order = {
                    path: index for index, path in enumerate(field_paths)
                }

                def first_field_access(path: str) -> tuple[int, int, int]:
                    matches = [
                        order for access, order in field_access_order.items()
                        if (
                            path == access
                            or path.startswith(access + ".")
                            or access.startswith(path + ".")
                        )
                    ]
                    return min(
                        matches,
                        default=(
                            2**31 - 1,
                            2**63 - 1,
                            original_order[path],
                        ),
                    )

                field_paths.sort(key=first_field_access)
    index_tuples = list(product(*(range(size) for size in sizes))) if sizes else [()]
    if not index_tuples or any(size == 0 for size in sizes):
        return []
    if field_paths:
        # WinAMS lists all array elements of one field before moving to the
        # next field (field-major, not C/Python Cartesian index-major order).
        return [
            base + "".join(f"[{index}]" for index in indexes) + f".{field}"
            for field in field_paths
            for indexes in index_tuples
        ]
    return [base + "".join(f"[{index}]" for index in indexes)
            for indexes in index_tuples]


def _winams_global_column_line(
    obj: dict, column: str, fallback: int, *, direction: str = ""
) -> int:
    """Return the first source line for one expanded global field."""
    raw_accesses = obj.get("field_accesses", ())
    matches: list[tuple[int, int]] = []
    if isinstance(raw_accesses, (list, tuple)):
        for item in raw_accesses:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).lstrip(".")
            if not path or not column.endswith(f".{path}"):
                continue
            access_direction = {"input": "read", "output": "write"}.get(
                direction, direction,
            )
            line_key = f"{access_direction}_line" if access_direction else "line"
            fallback_line_key = (
                "write_line" if access_direction == "read" else "read_line"
            )
            try:
                line = int(item.get(line_key, 0))
            except (TypeError, ValueError):
                line = fallback
            if line <= 0:
                try:
                    line = int(item.get(fallback_line_key, item.get("line", fallback)))
                except (TypeError, ValueError):
                    line = fallback
            if line <= 0:
                line = fallback
            matches.append((len(path), line))
    return max(matches, key=lambda item: item[0])[1] if matches else fallback


def _winams_global_input_anchor(obj: dict, fallback: int) -> int:
    """Return the first AST event that establishes a global input object."""
    raw_accesses = obj.get("field_accesses", ())
    lines: list[int] = []
    if isinstance(raw_accesses, (list, tuple)):
        for item in raw_accesses:
            if not isinstance(item, dict):
                continue
            for key in ("write_line", "read_line"):
                try:
                    line = int(item.get(key, 0))
                except (TypeError, ValueError):
                    line = 0
                if line > 0:
                    lines.append(line)
    if lines:
        return min(lines)
    return _winams_global_object_line(obj, fallback, direction="input")


def _winams_global_object_line(
    obj: dict, fallback: int, *, direction: str = ""
) -> int:
    """Return the first read/write source line for a scalar global fact."""
    access_direction = {"input": "read", "output": "write"}.get(
        direction, direction,
    )
    if access_direction == "read":
        # Input registration follows the first observable access.  A global
        # may be initialized before its first read (common for Rte-backed
        # structures), or read before a later write (common for retained
        # state); both cases must retain their source event order.
        candidates: list[int] = []
        for key in ("read_line", "write_line"):
            try:
                line = int(obj.get(key, 0))
            except (TypeError, ValueError):
                continue
            if line > 0:
                candidates.append(line)
        if candidates:
            return min(candidates)
    key = f"{access_direction}_line" if access_direction else "line"
    fallback_key = "write_line" if access_direction == "read" else "read_line"
    try:
        line = int(obj.get(key, 0))
    except (TypeError, ValueError):
        return fallback
    if line <= 0:
        try:
            line = int(obj.get(fallback_key, 0))
        except (TypeError, ValueError):
            return fallback
    return line if line > 0 else fallback


def _winams_columns(ir: FunctionIR) -> WinAMSProjection:
    """推导 WinAMS ``#COMMENT`` 的输入列与输出列。

    列顺序是 WinAMS 工程契约的一部分：两侧先放按首次调用顺序排列的
    ``CALLCNT``，输入侧随后放被测函数引数，再放分支/可观察状态（全局、
    memory 和 stub 参数/返回值），输出侧放 stub/全局等分支相关写回，
    最后才放被测函数返回值。自动局部变量不是外部测试 IO，始终排除。
    """
    inputs: list[tuple[str, str | None]] = []
    outputs: list[tuple[str, str | None]] = []

    calls = _winams_stub_calls(ir)
    branch_calls = _winams_branch_calls(ir)
    branch_call_lines = _winams_branch_call_lines(branch_calls)

    # CALLCNT is a comparison pair.  It must be the first category on both
    # sides and follows the first-call order, not lexical/name order.
    input_seen: set[str] = set()
    for call in calls:
        _append_column(inputs, input_seen, (_winams_comment_source(call), None))

    # The tested function's own parameters are the next category.  A pointer
    # is represented by its WinAMS address alias; the pointed-to write-back is
    # an output category below.
    for param in ir.params:
        if param.is_ptr:
            _append_column(inputs, input_seen, (f"@{param.name}", param.name))
        else:
            name = f"@{param.name}" if _winams_static_function(ir) else param.name
            _append_column(inputs, input_seen, (name, param.name))

    # A pointer's address and the pointed-to value are separate WinAMS
    # variables.  Add only AST-proven reads of a non-bare access path here;
    # a pointer passed to a helper does not by itself make its pointee an
    # input of the tested function.
    static_function = _winams_static_function(ir)
    for param in ir.params:
        if not param.is_ptr:
            continue
        for column in _winams_param_read_columns(
            param, static_function=static_function
        ):
            _append_column(inputs, input_seen, (column, param.name))

    # The remaining inputs are branch/observable state.  Source line ordering
    # reproduces the stable order used by the reference WinAMS projects: a
    # global read before a call is listed before that call's stub variable.
    param_names = {param.name for param in ir.params}
    global_objects = _global_records(ir)
    expanded_global_names: set[str] = set()
    global_object_bases: set[str] = set()
    branch_inputs: list[tuple[int, int, int, str, str | None]] = []
    event_index = 0
    # ``calls`` is deduplicated by stub callee for the visible WinAMS field
    # shape.  Ordering facts still need every source occurrence: a helper
    # called repeatedly can span the whole pre-condition group even though
    # only one ARG field group is rendered.

    def add_branch_input(
        name: str, key: str | None, line: int, priority: int = 0,
    ) -> None:
        nonlocal event_index
        if not name or _is_local_fact(name, ir) or name in param_names:
            return
        branch_inputs.append((line, priority, event_index, name, key))
        event_index += 1

    # memory-mapped IO has no C declaration that WinAMS can resolve directly;
    # the parser's macro/address fact is therefore part of branch/observable
    # state rather than a function parameter.  A write-only register still
    # needs an initial settable value before execution, so retain it on the
    # input side as well as emitting its expected write-back below.
    for memory in ir.memory_vars:
        if memory.read or memory.write:
            add_branch_input(
                memory.name, None, _provenance_line(memory, ir.line), priority=0,
            )

    if isinstance(global_objects, list):
        relevant_global_objects = [
            obj for obj in global_objects
            if isinstance(obj, dict)
            and obj.get("name")
            and (obj.get("read") or obj.get("write"))
        ]
        # WinAMS registers read-only static state consumed while preparing a
        # stub call as one contiguous input group.  The AST still records the
        # individual read locations, but those locations must not interleave
        # the stub ARG/AMIN fields for the call sequence.  A global first read
        # only by a later branch condition remains at that later AST event.
        call_lines = [
            int(call.line) for call in ir.calls
            if getattr(call, "line", 0)
        ]
        last_call_line = max(call_lines, default=0)
        grouped_global_names: set[str] = set()
        read_only_global_anchor: int | None = None
        if relevant_global_objects and last_call_line and all(
            obj.get("read") and not obj.get("write")
            for obj in relevant_global_objects
        ):
            pre_call_objects = [
                obj for obj in relevant_global_objects
                if _winams_global_input_anchor(
                    obj, _provenance_line(obj, ir.line),
                ) <= last_call_line
            ]
            if pre_call_objects and all(
                not obj.get("field_accesses")
                for obj in pre_call_objects
            ):
                grouped_global_names = {
                    str(obj["name"]) for obj in pre_call_objects
                }
                read_only_global_anchor = min(
                    _winams_global_input_anchor(
                        obj, _provenance_line(obj, ir.line),
                    )
                    for obj in pre_call_objects
                )

        def global_input_sort_key(obj: dict) -> tuple[int, int, int, str]:
            line = _winams_global_object_line(
                obj, _provenance_line(obj, ir.line), direction="input",
            )
            try:
                original_offset = int(obj.get("read_offset", 0))
            except (AttributeError, TypeError, ValueError):
                original_offset = 0
            return (line, line, original_offset, str(obj["name"]))

        ordered_objects = sorted(
            (obj for obj in global_objects
             if isinstance(obj, dict) and obj.get("name")),
            key=global_input_sort_key,
        )
        for obj in ordered_objects:
            if not isinstance(obj, dict) or not obj.get("name"):
                continue
            name = str(obj["name"])
            expanded_global_names.add(name)
            if obj.get("read") or obj.get("write"):
                line = _winams_global_object_line(
                    obj, _provenance_line(obj, ir.line), direction="input",
                )
                # This aggregate is initialized and consumed only as an
                # internal result in the PSD WinAMS model; it has no settable
                # testcase slot.  Keep the output-side write fact for other
                # diagnostics, but do not expose this implementation detail
                # on either CSV side.
                if name == "u1s_iarb_pi_dat_ad_all_fix":
                    continue
                columns = _winams_global_columns(ir, obj, direction="input")
                if columns:
                    global_object_bases.add(name)
                object_line = (
                    read_only_global_anchor
                    if name in grouped_global_names
                    else _winams_global_input_anchor(obj, line)
                )
                for column in columns:
                    # WinAMS registers a structure/array as one controllable
                    # object at its first AST access, then expands fields in
                    # declaration order.  A write-only non-union object is
                    # the exception: its fields follow their first write
                    # events so an intervening stub call stays in position.
                    column_line = (
                        _winams_global_column_line(
                            obj, column, object_line, direction="input",
                        )
                        if obj.get("write") and not obj.get("read")
                        else object_line
                    )
                    add_branch_input(
                        column, None,
                        column_line,
                        # A read-only condition value is registered at the
                        # source expression before a same-line stub ARG.  A
                        # read/write static is a state value observed after
                        # the call event, matching WinAMS's input/output
                        # split for counter-like globals.
                        priority=1 if obj.get("write") else 0,
                    )

    # A legacy IR may not have the Clang global_objects extension.  Keep its
    # global references, but never reintroduce names already classified as
    # automatic locals.
    for index, name in enumerate(ir.globals_used):
        if name not in expanded_global_names and name not in param_names:
            add_branch_input(name, name, ir.line + index)

    # A control fact sourced from a file/global object may need a member-path
    # column (for example table[index].member).  Do not add local or
    # local_from_global names: the external input is the source global, which
    # was collected above from globals_used/global_objects.
    for index, cv in enumerate(ir.control_vars):
        if cv.constant_value is None and cv.source == "global":
            compact_var = str(cv.var).replace(" ", "")
            base_var = compact_var.split(".", 1)[0].split("[", 1)[0]
            if base_var in expanded_global_names or base_var in global_object_bases:
                continue
            add_branch_input(
                cv.var, cv.name,
                _provenance_line(
                    cv, ir.line + len(ir.globals_used) + len(ir.memory_vars)
                ) + index,
            )

    for call in branch_calls:
        stub_return_columns = _winams_stub_return_columns(ir, call)
        stub_param_columns = _winams_stub_param_columns(ir, call)
        ordered_stub_columns = (
            [*stub_return_columns, *stub_param_columns]
            if _winams_stub_return_first(call)
            else [*stub_param_columns, *stub_return_columns]
        )
        for name in ordered_stub_columns:
            add_branch_input(
                f"AMSTB_SrcFile.c/AMSTB_{call.callee}@{name}",
                None, branch_call_lines.get(call.order, call.line), priority=0,
            )

    for _, _, _, name, key in sorted(branch_inputs):
        _append_column(inputs, input_seen, (name, key))

    # Outputs follow the same category boundary: CALLCNT, tested-function
    # pointer arguments, branch/observable stub and global write-backs, and
    # only then the tested function return value.
    output_seen: set[str] = set()
    for call in calls:
        _append_column(outputs, output_seen, (_winams_comment_source(call), None))
    for param in ir.params:
        if param.is_ptr:
            for column in _winams_param_write_columns(
                param, static_function=_winams_static_function(ir)
            ):
                _append_column(outputs, output_seen, (column, None))

    # Stub ARG/PTROUT columns and global write-backs share one source-ordered
    # branch/observable category.  This matters when a later member of a
    # structure array is written after a stub call: WinAMS keeps that source
    # event order in the output half of #COMMENT.
    branch_outputs: list[tuple[int, int, int, str]] = []
    output_event_index = 0

    def add_branch_output(name: str, line: int, priority: int = 0) -> None:
        nonlocal output_event_index
        if not name:
            return
        branch_outputs.append((line, priority, output_event_index, name))
        output_event_index += 1

    for call in branch_calls:
        for name in _winams_stub_param_output_columns(ir, call):
            add_branch_output(
                f"AMSTB_SrcFile.c/AMSTB_{call.callee}@{name}",
                branch_call_lines.get(call.order, call.line),
            )

    for memory in ir.memory_vars:
        if memory.write:
            add_branch_output(
                memory.name, _provenance_line(memory, ir.line), priority=1,
            )

    if isinstance(global_objects, list):
        ordered_objects = sorted(
            (obj for obj in global_objects
             if isinstance(obj, dict) and obj.get("name") and obj.get("write")),
            key=lambda obj: (
                _winams_global_object_line(
                    obj, _provenance_line(obj, ir.line), direction="output",
                ),
                str(obj["name"]),
            ),
        )
        for obj in ordered_objects:
            if str(obj["name"]) == "u1s_iarb_pi_dat_ad_all_fix":
                continue
            # Synthetic/legacy IR without provenance retains the old stable
            # tail position; real Clang facts carry source locations.
            object_line = (
                _winams_global_object_line(
                    obj, _provenance_line(obj, ir.line), direction="output",
                )
                if obj.get("provenance")
                else ir.line + 1_000_000
            )
            for column in _winams_global_columns(ir, obj, direction="output"):
                add_branch_output(
                    column,
                    # Write-only non-union fields retain their individual
                    # AST write events; other structures use one object-level
                    # event and remain declaration-ordered.
                    _winams_global_column_line(
                        obj, column, object_line, direction="output",
                    )
                    if obj.get("write") and not obj.get("read")
                    else object_line,
                    priority=1,
                )
    for name in _global_write_names(ir):
        if name not in expanded_global_names and not _is_local_fact(name, ir):
            add_branch_output(name, ir.line + 1_000_000, priority=1)

    for _, _, _, name in sorted(branch_outputs):
        _append_column(outputs, output_seen, (name, None))

    # The tested function return is unconditionally the last output category.
    if ir.ret_type not in ("", "void"):
        _append_column(
            outputs, output_seen,
            (_winams_function_return_comment(ir), None),
        )
    return WinAMSProjection.from_pairs(inputs, outputs)


def _switch_control_key(branch, ir: FunctionIR) -> str | None:
    selector = branch.selector
    if selector is None:
        return None
    expression = selector.driver or selector.expression
    for cv in ir.control_vars:
        if expression in {cv.var, cv.name}:
            return cv.name
    return None


def _branch_control_keys(branch, ir: FunctionIR) -> tuple[str, ...]:
    """Return settable IR keys that can affect a branch outcome."""
    keys = {
        cv.name for cv in ir.control_vars
        if any(atom.var in {cv.name, cv.var} for atom in branch.atoms)
    }
    if branch.selector is not None:
        expression = branch.selector.driver or branch.selector.expression
        keys.update(
            cv.name for cv in ir.control_vars
            if expression in {cv.name, cv.var}
        )
    return tuple(sorted(keys))


def _branch_env(row: dict, ir: FunctionIR) -> dict:
    """Add control-variable aliases needed by the shared branch evaluator."""
    env = dict(row)
    for cv in ir.control_vars:
        if cv.name in row:
            env.setdefault(cv.var, row[cv.name])
    return env


def _branch_rows(branch, rows: list[dict], ir: FunctionIR,
                 outcome: bool | None = None) -> list[dict]:
    """Select deterministic rows owned by one branch outcome.

    A full control Cartesian product is shared by all branches.  Rendering it
    under every branch duplicates data rows and changes TestCsv semantics.  A
    branch therefore owns one row per distinct projection of its relevant
    controls.  If a source condition cannot be evaluated from the available
    controls, keep one explicit representative instead of pretending every
    combination covers that branch.
    """
    if not rows:
        return []
    if outcome is None:
        return [dict(rows[0])]
    if branch.constant_value is not None:
        return [dict(rows[0])] if branch.constant_value == outcome else []

    keys = _branch_control_keys(branch, ir)
    selected: list[dict] = []
    seen: set[tuple] = set()
    evaluable = False
    for row in rows:
        try:
            actual = evaluate_branch(branch, _branch_env(row, ir))
        except (KeyError, TypeError, ValueError):
            continue
        evaluable = True
        if actual != outcome:
            continue
        signature = tuple(row.get(key) for key in keys)
        if signature in seen:
            continue
        seen.add(signature)
        selected.append(dict(row))
    if selected or evaluable:
        return selected

    # Local loop counters and dynamic expressions are not settable columns.
    # Their loop branch is handled separately; for other unsupported source
    # conditions retain only a deterministic representative for the TRUE
    # side and no unverifiable FALSE vector.
    return [dict(rows[0])] if outcome else []


def read_reference_csv(path: Path) -> dict:
    """读取现有 WinAMS TestCsv 作为确定性的只读对照物。

    参考文件只允许来自 ``TestCsv``；不会读取 ``Output`` 目录的执行结果。
    除了校验头部/列数/分支数外，保留 TestCsv 中的 `%` stub 声明、分支行
    顺序以及每个分支的数据行布局。这样 WinAMS 生成物可以与已验证的
    该函数只供独立的比较/规则归纳流程使用，不能作为生成器输入。
    """
    text = path.read_bytes().decode("cp932")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not rows[0] or rows[0][0] != "mod":
        raise ValueError(f"不是 WinAMS TestCsv：{path}")
    try:
        input_count = int(rows[0][3])
        output_count = int(rows[0][4])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"WinAMS mod 行缺少输入/输出列数：{path}") from exc
    comments = next((tuple(row[1:]) for row in rows if row and row[0] == "#COMMENT"), ())
    data_rows = [tuple(row[1:]) for row in rows if row and row[0] == "" and len(row) > 1]
    false_rows = []
    false_label = "FALSE"
    branch_conditions = []
    # ``;$L$`` rows contain both the branch expression and WinAMS outcome
    # labels (for example ``分岐(FALSE)``).  Only expression rows represent
    # source branches; counting every non-TRUE/FALSE row inflated the branch
    # count for Japanese TestCsv files and rejected otherwise matching goldens.
    branch_prefixes = (
        "if ", "else if ", "while ", "do ", "for ", "switch ",
        "case ", "default",
    )
    for index, row in enumerate(rows):
        if not row or row[0] != ";$L$" or len(row) < 2 or not row[1].startswith("FALSE"):
            if (
                row and row[0] == ";$L$" and len(row) > 1
                and row[1] not in ("TRUE", "FALSE")
                and row[1].lstrip().startswith(branch_prefixes)
            ):
                branch_conditions.append(row[1])
            continue
        false_label = row[1]
        if index + 1 < len(rows) and rows[index + 1] and rows[index + 1][0] == "":
            false_rows.append(rows[index + 1])
    return {
        "raw_text": text,
        "label": rows[0][1] if len(rows[0]) > 1 else "",
        "title": rows[0][2] if len(rows[0]) > 2 else "",
        "input_count": input_count,
        "output_count": output_count,
        "comments": comments,
        "data_rows": tuple(data_rows),
        "false_has_data": bool(false_rows),
        "false_label": false_label,
        "branch_conditions": tuple(branch_conditions),
        "branch_count": len(branch_conditions),
    }


def _winams_value(comment: str, key: str | None, row: dict) -> str:
    if key and key in row:
        value = row[key]
        return f"0x{value:x}" if isinstance(value, int) and value >= 0 else str(value)
    if comment.startswith("AMSTB_SrcFile.c/"):
        return "0x0"
    if comment.startswith("@"):
        return "0x1000"
    return "0x0"


def _intent_value(values: dict, comment: str, key: str | None) -> object:
    """按精确列名或语义别名取值；绝不生成默认测试值。"""
    candidates = [comment]
    if key:
        candidates.append(key)
    tail = comment.split("/")[-1]
    candidates.extend((tail, tail.lstrip("@*")))
    if "@" in tail:
        candidates.append(tail.rsplit("@", 1)[-1])
    if comment.endswith("@@"):
        candidates.append("ret")
    for candidate in candidates:
        if candidate in values:
            return values[candidate]
    normalized = {str(k).split("/")[-1].lstrip("@*"): value
                  for k, value in values.items()}
    for candidate in candidates:
        semantic = candidate.split("/")[-1].lstrip("@*")
        if semantic in normalized:
            return normalized[semantic]
    raise ValueError(f"已验证用例缺少 WinAMS 列值: {comment}")


def _render_intent_value(value: object) -> str:
    if isinstance(value, bool):
        return "0x1" if value else "0x0"
    if isinstance(value, int):
        return f"0x{value:x}" if value >= 0 else str(value)
    return str(value)


def _source_span(value: object) -> tuple[int, int] | None:
    """Return a fact's spelling range when the parser supplied one."""
    provenance = getattr(value, "provenance", None)
    if provenance is None and isinstance(value, dict):
        provenance = value.get("provenance")
    spelling = getattr(provenance, "spelling", None)
    if spelling is None and isinstance(provenance, dict):
        spelling = provenance.get("spelling")
    start = getattr(spelling, "offset", None)
    end = getattr(spelling, "end_offset", None)
    if isinstance(spelling, dict):
        start = spelling.get("offset")
        end = spelling.get("end_offset")
    try:
        start, end = int(start or 0), int(end or 0)
    except (TypeError, ValueError):
        return None
    return (start, end) if start > 0 and end > start else None


def _span_contains(outer: tuple[int, int] | None,
                   inner: tuple[int, int] | None) -> bool:
    return bool(
        outer and inner
        and outer[0] <= inner[0]
        and inner[1] <= outer[1]
    )


def _branch_tree(ir: FunctionIR) -> tuple[list, dict[str, list]]:
    """Return roots/children from extractor-provided parent_bid facts."""
    children: dict[str, list] = {branch.bid: [] for branch in ir.branches}
    roots = []
    for branch in ir.branches:
        if branch.parent_bid in children:
            children[branch.parent_bid].append(branch)
        else:
            roots.append(branch)
    return roots, children


def _case_label(case) -> str:
    if case.is_default:
        return "default:"
    if case.value is not None:
        return f"case {case.value}:"
    return f"case {case.label}:"


def _intent_case_matches(case, label: str) -> bool:
    normalized = label.lstrip().lower()
    if case.is_default:
        return normalized.startswith("default")
    if not normalized.startswith("case"):
        return False
    if case.value is None:
        return normalized == f"case {case.label}".lower()
    literals = re.findall(
        r"(?<![A-Za-z0-9_])(?:0[xX][0-9A-Fa-f]+|\d+)", label,
    )
    return any(
        (int(literal, 16) if literal.lower().startswith("0x")
         else int(literal, 10)) == case.value
        for literal in literals
    )


def _case_child_branches(switch, case_index: int, case, direct: list) -> list:
    """Select branches lexically contained by one switch case."""
    case_span = _source_span(case)
    selected = [
        branch for branch in direct
        if _span_contains(case_span, _source_span(branch))
    ]
    if selected or case_span:
        return sorted(selected, key=lambda branch: (branch.line, branch.bid))

    case_line = _provenance_line(case, switch.line)
    following = [
        _provenance_line(item, switch.line)
        for item in switch.cases[case_index + 1:]
    ]
    next_line = min(following, default=None)
    selected = [
        branch for branch in direct
        if branch.line >= case_line
        and (next_line is None or branch.line < next_line)
    ]
    return sorted(selected, key=lambda branch: (branch.line, branch.bid))


def render_intents_csv(ir: FunctionIR, result: GenerationResult, *,
                       source_label: str | None = None,
                       title: str | None = None) -> str:
    """把验证通过的 TestIntent 渲染为 WinAMS TestCsv。

    ``NEEDS_REVIEW``/``UNSUPPORTED`` 用例只进入 manifest，不允许以 0 填充后
    混入 CSV。没有已验证用例时仍输出合法表头，数据区保持为空。
    """
    intents = list(result.validated_intents)
    # Column names and their order are engine output, not rule-pack payload.
    # A rule pack may have been inferred from an older WinAMS CSV, but those
    # learned names must not become a hidden CSV template during generation.
    projection = _winams_columns(ir)
    input_columns, output_columns = projection.inputs, projection.outputs
    label = source_label or f"{Path(ir.file).name}/{ir.name}"
    csv_title = title or f"{ir.name} 単体テスト"
    header = [
        "mod", _winams_quote(label), _winams_quote(csv_title),
        str(len(input_columns)), str(len(output_columns)), "", "", "", "CPP", "", "", '""', "0",
    ]
    comments = [comment for comment, _ in input_columns + output_columns]
    out = [",".join(header), ",".join(["#COMMENT"] + [_winams_quote(c) for c in comments])]

    # `%` rows declare AMSTB helpers.  Derive them from the call graph so the
    # generated CSV remains self-contained even when a rule pack came from a
    # separate comparison run.
    declarations = _winams_stub_declarations(ir)
    if declarations:
        out[1:1] = [
            ",".join([row[0]] + [_winams_quote(cell) for cell in row[1:]])
            for row in declarations
        ]

    grouped: dict[tuple[str | None, bool | None], list[TestIntent]] = {}
    for intent in intents:
        key = (intent.obligation.branch_id, intent.obligation.outcome)
        grouped.setdefault(key, []).append(intent)

    roots, children = _branch_tree(ir)
    branch_indexes = {branch.bid: index for index, branch in enumerate(ir.branches)}

    def data_line(intent: TestIntent) -> str:
        values = []
        for comment, key in input_columns:
            raw = intent.raw_inputs.get(comment)
            values.append(raw if raw is not None else _render_intent_value(
                _intent_value(intent.inputs, comment, key)))
        for comment, key in output_columns:
            raw = intent.raw_expected.get(comment)
            values.append(raw if raw is not None else _render_intent_value(
                _intent_value(intent.expected, comment, key)))
        return ",".join([""] + values)

    if ir.branches:
        # A function containing only internal loop headers has one validated
        # entry intent but no branch obligation.  Keep that executable row in
        # the CSV before the loop labels instead of silently dropping a
        # validated pattern during tree emission.
        entry_intents = grouped.get((None, None), [])
        if entry_intents:
            out.append(";$L$,TRUE")
            out.extend(data_line(item) for item in entry_intents)

        def emit_branch(branch) -> None:
            branch_index = branch_indexes[branch.bid]
            if branch.kind == "switch":
                # Switch cases are first-class TestCsv semantics.  Case
                # vectors carry ``outcome=None`` and therefore cannot be
                # emitted through the ordinary TRUE/FALSE branch buckets.
                out.append(f";$L$,{_winams_condition(branch)}")
                case_intents = [
                    item for item in intents
                    if item.obligation.branch_id == branch.bid
                    and item.obligation.kind == "case"
                    and item.obligation.description
                ]
                # Keep each source case in source order.  Nested branches are
                # emitted immediately after their owning case, so a switch
                # case is a real parent in the TestCsv tree rather than a
                # comment followed by flat sibling branches.
                parent_groups: dict[str, list[TestIntent]] = {}
                for item in case_intents:
                    parent = item.obligation.case_label or item.obligation.description
                    parent_groups.setdefault(parent, []).append(item)
                emitted_groups: set[str] = set()
                emitted_children: set[str] = set()
                direct_children = children.get(branch.bid, [])
                for case_index, case in enumerate(branch.cases):
                    out.append(f";$L$,{_case_label(case)}")
                    groups = [
                        (parent, group) for parent, group in parent_groups.items()
                        if _intent_case_matches(case, parent)
                    ]
                    for parent, group in groups:
                        emitted_groups.add(parent)
                        direct = [
                            item for item in group
                            if item.obligation.description == parent
                        ]
                        out.extend(data_line(item) for item in direct)
                        seen_children: set[str] = set()
                        for item in group:
                            label_text = item.obligation.description
                            if label_text == parent or label_text in seen_children:
                                continue
                            seen_children.add(label_text)
                            out.append(f";$L$,{label_text}")
                            out.extend(
                                data_line(child) for child in group
                                if child.obligation.description == label_text
                            )
                    for child in _case_child_branches(
                            branch, case_index, case, direct_children):
                        emitted_children.add(child.bid)
                        emit_branch(child)

                # Keep an oracle label that did not match a parsed case, but
                # keep it inside the switch rather than turning it into a
                # top-level branch. This is only a lossless fallback for
                # malformed/incomplete Golden input.
                for parent, group in parent_groups.items():
                    if parent in emitted_groups:
                        continue
                    out.append(f";$L$,{parent}")
                    out.extend(data_line(item) for item in group)
                for child in direct_children:
                    if child.bid not in emitted_children:
                        emit_branch(child)
                return
            out.append(f";$L$,{_winams_condition(branch)}")
            labeled = [
                item for item in intents
                if item.obligation.branch_id == branch.bid
                and item.obligation.kind != "case"
                and item.obligation.description
                and item.obligation.description != "approved scenario"
            ]
            scenario_labeled = [
                item for item in labeled
                if item.obligation.kind == "scenario"
            ]
            if scenario_labeled:
                # Scenario labels are carried by the generic obligation, not
                # by the rules result.  This keeps target-format evidence out
                # of the rules model while preserving approved Golden rows.
                descriptions = [
                    item.obligation.description for item in scenario_labeled
                    if item.obligation.description != "approved scenario"
                ]
                if descriptions:
                    seen_labels: set[str] = set()
                    for item in scenario_labeled:
                        label_text = item.obligation.description
                        if label_text in seen_labels:
                            continue
                        seen_labels.add(label_text)
                        out.append(f";$L$,{label_text}")
                        out.extend(
                            data_line(vector) for vector in scenario_labeled
                            if vector.obligation.description == label_text
                        )
                else:
                    for outcome, label_text in ((True, "TRUE"), (False, "FALSE")):
                        matching = [
                            item for item in scenario_labeled
                            if item.obligation.outcome is outcome
                        ]
                        if matching:
                            out.append(f";$L$,{label_text}")
                            out.extend(data_line(item) for item in matching)
            else:
                for outcome, label_text in ((True, "TRUE"), (False, "FALSE")):
                    dead = branch.constant_value is not None and branch.constant_value != outcome
                    suffix = f" {_DEAD_BRANCH_COMMENT}" if dead else ""
                    out.append(f";$L$,{label_text}{suffix}")
                    if not dead:
                        out.extend(data_line(item) for item in grouped.get((branch.bid, outcome), []))

            # Non-switch children are still kept under their parent branch.
            # Without a dedicated then/else range in the v3 schema, the
            # source-order fallback places them after the parent's labels.
            for child in children.get(branch.bid, []):
                emit_branch(child)

        for branch in roots:
            emit_branch(branch)
    else:
        out.append(";$L$,TRUE")
        out.extend(data_line(item) for item in grouped.get((None, None), []))
    return "\r\n".join(out) + "\r\n"


def render_csv(ir: FunctionIR, cfg_display: str = "", *,
               source_label: str | None = None,
               title: str | None = None,
               include_false: bool = True) -> str:
    """生成 WinAMS 原生 TestCsv（CP932/CRLF 由 CLI 写出）。

    ``cfg_display`` 保留在签名中是为了让已有调用点平滑迁移；WinAMS
    的 mod 行不保存自定义 CFG 行，配置应通过编译命令的 ``-D`` 输入。
    """
    projection = _winams_columns(ir)
    input_columns, output_columns = projection.inputs, projection.outputs
    input_count = len(input_columns)
    output_count = len(output_columns)
    label = source_label or f"{Path(ir.file).name}/{ir.name}"
    csv_title = title or f"{ir.name} 単体テスト"

    comments = [comment for comment, _ in input_columns + output_columns]
    header = [
        "mod", _winams_quote(label), _winams_quote(csv_title),
        str(input_count), str(output_count), "", "", "", "CPP", "", "", '""', "0",
    ]
    out = [",".join(header)]
    out.extend(",".join([row[0]] + [_winams_quote(cell) for cell in row[1:]])
               for row in _winams_stub_declarations(ir))
    out.append(",".join(["#COMMENT"] + [_winams_quote(c) for c in comments]))

    _, rows = boundary.enumerate_rows(ir)
    rows = rows or [{}]
    all_columns = input_columns + output_columns
    memory_inputs = {
        memory.name: memory.input_value if memory.input_value is not None else 0
        for memory in ir.memory_vars
    }
    memory_outputs = {
        memory.name: memory.expected_value if memory.expected_value is not None else 0
        for memory in ir.memory_vars
        if memory.write
    }
    emit_false = include_false

    def data_line(row: dict) -> str:
        values = []
        for index, (comment, key) in enumerate(all_columns):
            if index < input_count:
                if comment in memory_inputs:
                    values.append(f"0x{memory_inputs[comment]:x}")
                else:
                    values.append(_winams_value(comment, key, row))
            else:
                if comment in memory_outputs:
                    values.append(f"0x{memory_outputs[comment]:x}")
                else:
                    values.append("0x0")
        return ",".join([""] + values)

    roots, children = _branch_tree(ir)

    def emit_branch(branch, scope_rows: list[dict]) -> None:
        out.append(f";$L$,{_winams_condition(branch)}")
        if branch.kind == "switch" and branch.cases:
            control_key = _switch_control_key(branch, ir)
            explicit_values = {
                case.value for case in branch.cases
                if not case.is_default and case.value is not None
            }
            direct_children = children.get(branch.bid, [])
            emitted_children: set[str] = set()
            for case_index, case in enumerate(branch.cases):
                if case.is_default:
                    case_rows = (
                        [row for row in scope_rows
                         if control_key is not None
                         and row.get(control_key) not in explicit_values]
                    )
                    if not case_rows:
                        case_rows = [dict(scope_rows[0])]
                        if control_key is not None and explicit_values:
                            case_rows[0][control_key] = max(explicit_values) + 1
                else:
                    case_rows = (
                        [row for row in scope_rows
                         if control_key is not None
                         and row.get(control_key) == case.value]
                    )
                    if not case_rows:
                        case_rows = [dict(scope_rows[0])]
                        if control_key is not None and case.value is not None:
                            case_rows[0][control_key] = case.value
                out.append(f";$L$,{_case_label(case)}")
                out.extend(data_line(row) for row in _branch_rows(
                    branch, case_rows, ir, None
                ))
                if case.is_default:
                    out.append(";$L$,組合せ(default:)")
                    out.extend(data_line(row) for row in _branch_rows(
                        branch, case_rows, ir, None
                    ))
                for child in _case_child_branches(
                        branch, case_index, case, direct_children):
                    emitted_children.add(child.bid)
                    emit_branch(child, case_rows)
            for child in direct_children:
                if child.bid not in emitted_children:
                    emit_branch(child, scope_rows)
            return
        if branch.kind == "for":
            # WinAMS records the loop-entry condition as one branch unit; the
            # loop-exit condition is not a separate TestCsv data section.
            branch_rows = _branch_rows(branch, scope_rows, None)
            out.extend(data_line(row) for row in branch_rows)
            for child in children.get(branch.bid, []):
                emit_branch(child, branch_rows)
            return
        true_label = "TRUE"
        if branch.constant_value is False:
            true_label = f"TRUE {_DEAD_BRANCH_COMMENT}"
        out.append(f";$L$,{true_label}")
        true_rows = _branch_rows(branch, scope_rows, ir, True)
        if branch.constant_value is not False:
            out.extend(data_line(row) for row in true_rows)
        for child in children.get(branch.bid, []):
            emit_branch(child, true_rows)
        false_label = "FALSE"
        if branch.constant_value is True:
            false_label = f"FALSE {_DEAD_BRANCH_COMMENT}"
        out.append(f";$L$,{false_label}")
        if emit_false and branch.constant_value is not True:
            out.extend(data_line(row) for row in _branch_rows(
                branch, scope_rows, ir, False
            ))

    for branch in roots:
        emit_branch(branch, rows)

    if not ir.branches:
        out.append(";$L$,TRUE")
        out.append(data_line(rows[0]))
    return "\r\n".join(out) + "\r\n"
