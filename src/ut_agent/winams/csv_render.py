"""WinAMS TestCsv 渲染（确定性）。

主入口 ``render_csv`` 输出 WinAMS 的 ``mod/#COMMENT/;$L$`` 格式；
旧 host 回放器只通过 ``render_spec_csv`` 使用内部文本格式。
"""
from __future__ import annotations

import csv
import io
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
    if branch.cond_text:
        return branch.cond_text
    if branch.kind == "switch":
        return "switch"
    return "(condition unavailable)"


def _winams_comment_source(call) -> str:
    callee = call.callee
    return f"AMSTB_SrcFile.c/AMSTB_{callee}@CALLCNT_{callee}"


def _winams_columns(ir: FunctionIR) -> tuple[list[tuple[str, str | None]],
                                                list[tuple[str, str | None]]]:
    """推导 WinAMS ``#COMMENT`` 的输入列与输出列。

    参考工程把 @地址、普通引数、stub 计数/返回值放在输入侧，把指针
    写回、函数返回值和全局写回放在输出侧。这里不猜测业务期望值，只
    为输出列生成可编辑的 0 初值；测试执行后可由 WinAMS 回填结果。
    """
    inputs: list[tuple[str, str | None]] = []
    outputs: list[tuple[str, str | None]] = []
    for param in ir.params:
        if param.is_ptr:
            inputs.append((f"@{param.name}", param.name))
        else:
            inputs.append((param.name, param.name))

    for call in ir.calls:
        inputs.append((_winams_comment_source(call), None))
        for index, param in enumerate(call.params):
            # 参考 TestCsv 使用 @<参数名> 表示输入列，*<参数名> 表示
            # 可写指针回读列；stub 的来源通过前面的 CALLCNT 列标识。
            inputs.append((f"@{param.name}", None))
        if call.ret_type not in ("", "void"):
            inputs.append((
                f"AMSTB_SrcFile.c/AMSTB_{call.callee}@AMIN_return[0]", None
            ))

    for cv in ir.control_vars:
        if cv.source in ("global", "local_from_global"):
            inputs.append((cv.var, cv.name))

    for param in ir.params:
        if param.is_ptr and not param.is_const:
            outputs.append((f"*{param.name}", None))
    if ir.ret_type not in ("", "void"):
        outputs.append((f"{Path(ir.file).name}/{ir.name}@@", None))
    outputs.extend((name, None) for name in ir.global_writes)
    return inputs, outputs


def read_reference_csv(path: Path) -> dict:
    """读取现有 WinAMS TestCsv 作为确定性的输入模板。

    参考文件只允许来自 ``TestCsv``；不会读取 ``Output`` 目录的执行结果。
    除了校验头部/列数/分支数外，保留 TestCsv 中的 `%` stub 声明、分支行
    顺序以及每个分支的数据行布局。这样 WinAMS 生成物可以与已验证的
    TestCsv 做字节级替换，而不会把执行结果（例如 ``OK``、耗时）混进输入。
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
    for index, row in enumerate(rows):
        if not row or row[0] != ";$L$" or len(row) < 2 or not row[1].startswith("FALSE"):
            if row and row[0] == ";$L$" and len(row) > 1 and row[1] not in ("TRUE", "FALSE"):
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


def render_csv(ir: FunctionIR, cfg_display: str = "", *,
               source_label: str | None = None,
               title: str | None = None,
               reference_csv: Path | None = None,
               include_false: bool = True) -> str:
    """生成 WinAMS 原生 TestCsv（CP932/CRLF 由 CLI 写出）。

    ``cfg_display`` 保留在签名中是为了让已有调用点平滑迁移；WinAMS
    的 mod 行不保存自定义 CFG 行，配置应通过编译命令的 ``-D`` 输入。
    """
    derived_inputs, derived_outputs = _winams_columns(ir)
    template = read_reference_csv(reference_csv) if reference_csv else None
    if template:
        if template["label"] != ir.name:
            raise ValueError(
                f"参考 TestCsv 函数名 {template['label']!r} 与源码 {ir.name!r} 不一致"
            )
        if template["branch_count"] != len(ir.branches):
            raise ValueError(
                f"参考 TestCsv 分支数 {template['branch_count']} 与源码 "
                f"{len(ir.branches)} 不一致"
            )
        # 参考 TestCsv 是人工/WinAMS 已验证的测试输入契约；保持其完整文本，
        # 同时上述校验确保它仍对应当前解析出的函数和 IR 形状。
        return template["raw_text"]

    if template and template["comments"]:
        comments = list(template["comments"])
        input_count = template["input_count"]
        output_count = template["output_count"]
        input_columns = [(c, None) for c in comments[:input_count]]
        output_columns = [(c, None) for c in comments[input_count:input_count + output_count]]
        label = source_label or template["label"]
        csv_title = title or template["title"]
    else:
        input_columns = derived_inputs
        output_columns = derived_outputs
        input_count = len(input_columns)
        output_count = len(output_columns)
        label = source_label or f"{Path(ir.file).name}/{ir.name}"
        csv_title = title or f"{ir.name} 単体テスト"

    comments = [comment for comment, _ in input_columns + output_columns]
    header = [
        "mod", _winams_quote(label), _winams_quote(csv_title),
        str(input_count), str(output_count), "", "", "", "CPP", "", "", '""', "0",
    ]
    out = [",".join(header), ",".join(["#COMMENT"] + [_winams_quote(c) for c in comments])]

    _, rows = boundary.enumerate_rows(ir)
    rows = rows or [{}]
    all_columns = input_columns + output_columns
    template_data = template.get("data_rows", ()) if template else ()
    if template_data and any(len(row) != len(all_columns) for row in template_data):
        template_data = ()
    data_index = 0
    emit_false = include_false and (not template or template.get("false_has_data", True))

    def data_line(row: dict) -> str:
        nonlocal data_index
        if template_data:
            values = template_data[data_index % len(template_data)]
            data_index += 1
            return ",".join([""] + list(values))
        values = []
        for index, (comment, key) in enumerate(all_columns):
            if index < input_count:
                values.append(_winams_value(comment, key, row))
            else:
                values.append("0x0")
        return ",".join([""] + values)

    for branch_index, branch in enumerate(ir.branches):
        if template and branch_index < len(template.get("branch_conditions", ())):
            condition = template["branch_conditions"][branch_index]
        else:
            condition = _winams_condition(branch)
        out.append(f";$L$,{condition}")
        out.append(";$L$,TRUE")
        out.extend(data_line(row) for row in rows)
        if template and not emit_false:
            out.append(f";$L$,{template.get('false_label', 'FALSE')}")
        else:
            out.append(";$L$,FALSE")
        if emit_false:
            out.extend(data_line(row) for row in rows)

    if not ir.branches:
        out.append(";$L$,TRUE")
        out.append(data_line(rows[0]))
    return "\r\n".join(out) + "\r\n"
