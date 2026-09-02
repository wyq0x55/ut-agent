"""从人工 WinAMS TestCsv 生成待审批的确定性候选规则。"""
from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
import re
from typing import Any

from ut_agent.ir import FunctionIR


def _value(text: str) -> Any:
    stripped = text.strip()
    try:
        return int(stripped, 0)
    except ValueError:
        try:
            return int(stripped)
        except ValueError:
            return stripped


def _value_class(value: Any) -> str:
    if not isinstance(value, int):
        return "symbol"
    if value == 0:
        return "zero"
    if value == 1:
        return "one"
    if value in (0xFF, 0xFFFF, 0xFFFFFFFF):
        return "type-max"
    if 0x1000 <= value <= 0xFFFF and value % 0x100 == 0:
        return "pointer-address"
    return "literal"


def _source_macro_values(path: Path) -> dict[str, int]:
    """读取源码树中可直接求值的整型宏，供数组下标绑定使用。"""
    values: dict[str, int] = {}
    root = Path(path)
    files = ([root] + sorted(root.parent.rglob("*.h"))) if root.is_file() else []
    define = re.compile(r"^\s*#define\s+([A-Za-z_]\w*)\s+(.+?)\s*$")
    for item in files:
        try:
            lines = item.read_bytes().decode("cp932", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = define.match(line.split("/*", 1)[0].split("//", 1)[0])
            if not match:
                continue
            expr = re.sub(r"\(\s*[A-Za-z_]\w*\s*\)", "", match.group(2))
            expr = expr.strip().strip("()").strip().rstrip("uUlL")
            try:
                values[match.group(1)] = int(expr, 0)
            except ValueError:
                continue
    return values


def _bind_expression_columns(
    ir: FunctionIR, comments: list[str], scenarios: list[dict[str, Any]]
) -> dict[str, str]:
    """将数组/结构体原子表达式绑定到历史 CSV 的全限定列。"""
    macros = _source_macro_values(Path(ir.file))
    bindings: dict[str, str] = {}
    input_columns = comments[:]
    for branch in ir.branches:
        for atom in branch.atoms:
            expression = atom.var or atom.text
            identifiers = re.findall(r"[A-Za-z_]\w*", expression)
            if not identifiers:
                continue
            base = identifiers[0]
            index_match = re.search(r"\[\s*([A-Za-z_]\w*|0[xX][0-9A-Fa-f]+|\d+)\s*\]", expression)
            index = None
            if index_match:
                token = index_match.group(1)
                try:
                    index = int(token, 0)
                except ValueError:
                    index = macros.get(token)
            member = None
            member_match = re.search(r"(?:[.]|->)\s*([A-Za-z_]\w*)\s*$", expression)
            if member_match:
                member = member_match.group(1)
            matches: list[str] = []
            for column in input_columns:
                tail = column.split("/", 1)[-1]
                compact = re.sub(r"\s+", "", tail)
                if not re.search(rf"(?<!\w){re.escape(base)}(?!\w)", compact):
                    continue
                if index is not None and not re.search(rf"\[{index}\]", compact):
                    continue
                if member is not None and not compact.endswith(member):
                    continue
                matches.append(column)
            if len(matches) != 1:
                continue
            column = matches[0]
            bindings[expression] = column
            for scenario in scenarios:
                if column in scenario["inputs"]:
                    scenario["inputs"][expression] = scenario["inputs"][column]
    return bindings


def _bind_indexed_input_columns(
    ir: FunctionIR, comments: list[str], scenarios: list[dict[str, Any]]
) -> dict[str, str]:
    """为 ``array[index]`` 全局量按场景下标绑定 WinAMS 输入列。"""
    bindings: dict[str, str] = {}
    expressions: list[str] = []
    for branch in ir.branches:
        for atom in branch.atoms:
            expressions.append(atom.var)
            expressions.extend(
                match.group(0) for match in re.finditer(
                    r"[A-Za-z_]\w*\s*\[\s*[A-Za-z_]\w*\s*\]", atom.text
                )
            )
    expressions.extend(cv.var for cv in ir.control_vars)
    for expression in expressions:
        match = re.match(r"\s*([A-Za-z_]\w*)\s*\[\s*([A-Za-z_]\w*)\s*\]", expression)
        if not match:
            continue
        base, index_name = match.group(1), match.group(2)
        candidates = [
            column for column in comments
            if re.search(rf"(?<!\w){re.escape(base)}\s*\[\s*(\d+)\s*\]", column)
        ]
        if not candidates:
            continue
        for item in scenarios:
            index = item["inputs"].get(index_name)
            if index is None:
                continue
            column = next(
                (candidate for candidate in candidates
                 if re.search(rf"\[\s*{int(index)}\s*\]", candidate)),
                None,
            )
            if column is None or column not in item["inputs"]:
                continue
            item["inputs"][expression] = item["inputs"][column]
            bindings.setdefault(expression, f"{base}[{index_name}]")
    return bindings


def _config_table_values(ir: FunctionIR) -> dict[str, list[dict[str, int]]]:
    """解析同目录静态配置表，返回逐下标的确定性字段值。"""
    source = Path(ir.file)
    values = _source_macro_values(source)
    result: dict[str, list[dict[str, int]]] = {}
    # 该结构的字段顺序来自同项目头文件 typedef；不将其写入 CSV，
    # 只作为源码配置证据参与场景分支求值。
    fields = ("u1_err_det", "u1_tmr_id", "u2_mask", "u1_err_clr", "u1_jdg_cnt", "u1_spi_req")
    for cfg in sorted(source.parent.glob("*_cfg.c")):
        text = _source_text(cfg)
        for match in re.finditer(
            r"(?P<name>[A-Za-z_]\w*)\s*\[[^\]]+\]\s*=\s*\{(?P<body>.*?)\};",
            text,
            flags=re.DOTALL,
        ):
            rows: list[dict[str, int]] = []
            for row in re.findall(r"\{([^{}\n]*)\}", match.group("body")):
                tokens = [item.strip() for item in row.split(",")]
                if len(tokens) < len(fields):
                    continue
                parsed: dict[str, int] = {}
                for field, token in zip(fields, tokens):
                    token = token.strip().strip("()")
                    try:
                        parsed[field] = int(token, 0)
                    except ValueError:
                        parsed[field] = values.get(token, 0 if any(
                            word in token.upper() for word in ("INVALID", "FALSE", "OFF", "OK", "ZERO", "NONE")
                        ) else 1)
                rows.append(parsed)
            if rows:
                result[match.group("name")] = rows
    return result


def _bind_config_table_columns(
    ir: FunctionIR, scenarios: list[dict[str, Any]]
) -> dict[str, str]:
    """将配置表成员表达式解析为按参数下标变化的场景输入别名。"""
    tables = _config_table_values(ir)
    bindings: dict[str, str] = {}
    if not tables:
        return bindings
    from ut_agent.rules.engine import _expanded_env
    expressions = [atom.var for branch in ir.branches for atom in branch.atoms]
    expressions.extend(cv.var for cv in ir.control_vars)
    for expression in expressions:
        match = re.search(
            r"([A-Za-z_]\w*)\s*\[\s*([A-Za-z_]\w*)\s*\]\s*\.\s*([A-Za-z_]\w*)",
            expression,
        )
        if not match or match.group(1) not in tables:
            continue
        table, index_name, field = match.groups()
        if field not in {"u1_err_det", "u2_mask", "u1_err_clr", "u1_jdg_cnt"}:
            continue
        for item in scenarios:
            env = _expanded_env(item["inputs"])
            try:
                index = int(env[index_name])
            except (KeyError, TypeError, ValueError):
                continue
            rows = tables[table]
            if not (0 <= index < len(rows)):
                continue
            value = rows[index][field]
            if "&" in expression:
                rhs = expression.split("&", 1)[1]
                try:
                    value &= int(_expanded_env(item["inputs"])[rhs.strip()])
                except (KeyError, TypeError, ValueError):
                    continue
            item["inputs"][expression] = value
        bindings.setdefault(expression, f"{table}[{index_name}].{field}")
    return bindings


def _bind_comparison_operands(
    ir: FunctionIR, comments: list[str], scenarios: list[dict[str, Any]]
) -> dict[str, str]:
    """绑定变量-变量比较右值（计数器、FET 计数等）到 CSV 列。"""
    bindings: dict[str, str] = {}
    for branch in ir.branches:
        for atom in branch.atoms:
            match = re.search(r"(?:==|!=|<=|>=|<|>)\s*(.+?)\s*$", atom.text)
            if not match:
                continue
            rhs = match.group(1).strip().strip("()")
            if not re.match(r"^[A-Za-z_]\w*(?:\s*\[\s*[A-Za-z_]\w*\s*\])?$", rhs):
                continue
            if "[" in rhs:
                continue  # indexed RHS 已由 _bind_indexed_input_columns 处理
            candidates = [column for column in comments if re.search(
                rf"(?<!\w){re.escape(rhs)}(?!\w)", column
            )]
            if len(candidates) != 1:
                continue
            column = candidates[0]
            for item in scenarios:
                if column in item["inputs"]:
                    item["inputs"][rhs] = item["inputs"][column]
            bindings[rhs] = column
    return bindings


def _column_related(variable: str, column: str) -> bool:
    """限制相关性推断，禁止仅凭真假相关性绑定任意列。"""
    compact = re.sub(r"[^a-z0-9_]", "_", column.lower())
    name = variable.lower()
    if name in compact:
        return True
    generic = {"u1a", "u1s", "u1l", "u2a", "u2s", "dat", "flg"}
    tokens = [token for token in re.split(r"_+", name) if len(token) >= 2]
    return any(
        token not in generic
        and re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", compact)
        for token in tokens
    )


def _bind_stub_call_columns(ir: FunctionIR, comments: list[str], scenarios: list[dict[str, Any]]) -> dict[str, str]:
    """为 CALLCNT 建立被测函数调用名与 WinAMS 列名之间的别名。"""
    bindings: dict[str, str] = {}
    for call in ir.calls:
        wanted = f"CALLCNT_{call.callee}"
        candidates = [
            column for column in comments
            if "CALLCNT_" in column and _stub_call_column_match(call.callee, column)
        ]
        if len(candidates) != 1:
            continue
        column = candidates[0]
        bindings[wanted] = column
        for scenario in scenarios:
            if column in scenario["inputs"]:
                scenario["inputs"][wanted] = scenario["inputs"][column]
    return bindings


def _source_text(path: Path) -> str:
    """读取源码文本；规则采集只把源码作为确定性证据，不调用外部服务。"""
    try:
        return path.read_bytes().decode("cp932", errors="replace")
    except OSError:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _amin_return_match(callee: str, column: str) -> bool:
    """判断 AMIN_return 列是否属于源码中的调用（允许 AMSTB 前缀）。"""
    if "AMIN_return" not in column:
        return False
    marker = column.split("@AMIN_return", 1)[0]
    marker = marker.rsplit("/", 1)[-1]
    marker = re.sub(r"^(?:AMSTB_|Rte_Call_|CDD_)", "", marker)
    wanted = re.sub(r"^(?:Rte_Call_|CDD_)", "", callee)
    return marker == wanted or marker.endswith("_" + wanted)


def _bind_derived_local_columns(
    ir: FunctionIR, comments: list[str], scenarios: list[dict[str, Any]]
) -> dict[str, Any]:
    """登记源码赋值推导出的局部量。

    WinAMS 会把 stub 的 AMIN_return 作为输入列，但 Clang 在缺失嵌套头文件
    时可能无法把该调用登记到 ``ir.calls``。同时，局部前置标志常由源码中的
    ``TRUE`` 初始化和嵌套条件改写为 ``FALSE``。这里仅复制可由源码/已解析
    原子确定的值，保留 ``derived_bindings`` 证据，绝不把未知值伪造成输入。
    """
    source = _source_text(Path(ir.file))
    if not source:
        return {}
    bindings: dict[str, Any] = {}

    # local = called_function(...): 绑定唯一同名 AMIN_return 列。
    for cv in ir.control_vars:
        if cv.source != "local":
            continue
        call_match = re.search(
            rf"\b{re.escape(cv.name)}\s*=\s*([A-Za-z_]\w*)\s*\(", source
        )
        if not call_match:
            continue
        callee = call_match.group(1)
        candidates = [column for column in comments if _amin_return_match(callee, column)]
        if len(candidates) != 1:
            continue
        column = candidates[0]
        bindings[cv.name] = column
        for item in scenarios:
            if column in item["inputs"]:
                item["inputs"][cv.name] = item["inputs"][column]

    # local = TRUE; ... if (A) { if (B) { local = FALSE; } }
    # 通过分支行号筛选这段赋值之间已解析的原子，按源码顺序求值。
    for cv in ir.control_vars:
        if cv.source != "local" or cv.name in bindings:
            continue
        assignments = list(re.finditer(
            rf"\b{re.escape(cv.name)}\s*=\s*([A-Za-z_]\w*|0[xX][0-9A-Fa-f]+|\d+)\s*;",
            source,
        ))
        if len(assignments) < 2:
            continue
        first, last = assignments[0], assignments[1]
        true_token, false_token = first.group(1), last.group(1)
        first_line = source.count("\n", 0, first.start()) + 1
        last_line = source.count("\n", 0, last.start()) + 1

        def _constant(token: str, default: int | None = None) -> int | None:
            try:
                return int(token, 0)
            except ValueError:
                pass
            values = _source_macro_values(Path(ir.file))
            if token in values:
                return values[token]
            upper = token.upper()
            if "TRUE" in upper:
                return 1
            if any(word in upper for word in ("FALSE", "OFF", "OK", "ZERO", "CLR")):
                return 0
            return default

        true_value = _constant(true_token)
        false_value = _constant(false_token)
        # WinAMS/C 源码中的 bool-like 标志通常是 TRUE/FALSE 宏；当宏定义在
        # 缺失的嵌套头文件中时，用该变量已解析的 TRUE 原子边界补足证据。
        if true_value is None:
            for branch in ir.branches:
                for atom in branch.atoms:
                    if atom.var == cv.name and atom.boundary is not None:
                        true_value = int(atom.boundary)
                        break
                if true_value is not None:
                    break
        if false_value is None and true_value in (0, 1):
            false_value = 1 - int(true_value)
        if true_value is None or false_value is None:
            continue

        between = source[first.end():last.start()]
        candidates: list[tuple[int, Any]] = []
        for branch in ir.branches:
            if not branch.atoms or not (first_line <= branch.line <= last_line + 1):
                continue
            normalized = re.sub(r"\s+", "", branch.cond_text or branch.atoms[0].text)
            if normalized and normalized in re.sub(r"\s+", "", between):
                candidates.append((branch.line, branch))
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0])
        from ut_agent.rules.engine import _expanded_env, evaluate_branch

        resolved = True
        for item in scenarios:
            env = _expanded_env(item["inputs"])
            try:
                nested_true = all(evaluate_branch(branch, env) for _, branch in candidates)
            except (KeyError, TypeError, ValueError):
                resolved = False
                break
            item["inputs"][cv.name] = false_value if nested_true else true_value
        if resolved:
            bindings[cv.name] = {
                "kind": "source-derived-condition",
                "initial": true_token,
                "override": false_token,
                "guard_branches": [branch.bid for _, branch in candidates],
            }
    return bindings


def _bind_output_columns(
    ir: FunctionIR, output_columns: list[str], scenarios: list[dict[str, Any]]
) -> dict[str, str]:
    """按函数写入表达式和场景下标补齐动态输出 Oracle 别名。"""
    bindings: dict[str, str] = {}
    targets = list(ir.global_writes)
    targets.extend(param.name for param in ir.params if param.is_ptr and param.is_written)
    for target in targets:
        identifiers = re.findall(r"[A-Za-z_]\w*", target)
        if not identifiers:
            continue
        base = identifiers[0]
        index_match = re.search(r"\[\s*([A-Za-z_]\w*)\s*\]", target)
        index_name = index_match.group(1) if index_match else None
        candidates = [
            column for column in output_columns
            if re.search(rf"(?<!\w){re.escape(base)}(?!\w)", column)
        ]
        if not candidates:
            continue
        for scenario in scenarios:
            column = None
            if index_name:
                index = scenario["inputs"].get(index_name)
                if index is not None:
                    column = next(
                        (item for item in candidates if re.search(rf"\[{int(index)}\]", item)),
                        None,
                    )
            elif len(candidates) == 1:
                column = candidates[0]
            if column is not None and column in scenario["expected"]:
                scenario["expected"][target] = scenario["expected"][column]
                bindings[target] = (
                    column if not index_name else f"{base}[{index_name}]"
                )
    return bindings


def _stub_call_column_match(callee: str, column: str) -> bool:
    """匹配 WinAMS 中可能带 CDD/AMSTB 前缀的 CALLCNT 列。"""
    wanted = callee.split("@", 1)[-1]
    wanted = re.sub(r"^(?:Rte_Call_|CDD_)", "", wanted)
    marker = column.split("@CALLCNT_", 1)[-1]
    marker = re.sub(r"^(?:Rte_Call_|CDD_)", "", marker)
    return marker == wanted or marker.endswith("_" + wanted)


def semantic_csv_signature(path: Path) -> dict[str, Any]:
    """返回与行序、数值格式无关的 WinAMS 用例语义签名。"""
    rows = list(csv.reader(io.StringIO(path.read_bytes().decode("cp932"))))
    if not rows or not rows[0] or rows[0][0] != "mod":
        raise ValueError(f"不是 WinAMS TestCsv: {path}")
    input_count = int(rows[0][3])
    output_count = int(rows[0][4])
    comments = next((tuple(row[1:]) for row in rows if row and row[0] == "#COMMENT"), ())
    branch = "ENTRY"
    outcome = "TRUE"
    cases: list[tuple[str, str, tuple[tuple[str, str, Any], ...]]] = []
    for row in rows:
        if row and row[0] == ";$L$" and len(row) > 1:
            label = row[1]
            if label.startswith("TRUE"):
                outcome = "TRUE"
            elif label.startswith("FALSE"):
                outcome = "FALSE"
            else:
                branch = re.sub(r"\s+", "", label)
            continue
        if not row or row[0] != "" or len(row) <= 1:
            continue
        values = [_value(item) for item in row[1:1 + input_count + output_count]]
        typed = tuple(sorted(
            (comment, _value_class(value), value)
            for comment, value in zip(comments, values)
        ))
        cases.append((branch, outcome, typed))
    return {
        "input_columns": tuple(sorted(comments[:input_count])),
        "output_columns": tuple(sorted(comments[input_count:input_count + output_count])),
        "cases": tuple(sorted(cases, key=repr)),
    }


def infer_rule_pack(ir: FunctionIR, golden: Path) -> dict[str, Any]:
    data = golden.read_bytes()
    rows = list(csv.reader(io.StringIO(data.decode("cp932"))))
    if not rows or not rows[0] or rows[0][0] != "mod":
        raise ValueError(f"不是 WinAMS TestCsv: {golden}")
    input_count = int(rows[0][3])
    output_count = int(rows[0][4])
    comments = next((row[1:] for row in rows if row and row[0] == "#COMMENT"), [])
    if len(comments) < input_count + output_count:
        raise ValueError("#COMMENT 列数少于 mod 声明")
    current_branch: int | None = None
    current_outcome: bool | None = None
    current_case: str | None = None
    observed_labels: list[str] = []
    branch_labels: list[str] = []
    golden_outcome_labels: dict[int, list[str]] = {}
    stub_declarations: list[list[str]] = []
    branch_index = -1
    scenarios = []
    current_vector_label: str | None = None
    for row in rows:
        if row and row[0] == "%":
            stub_declarations.append(list(row))
            continue
        if row and row[0] == ";$L$" and len(row) > 1:
            label = row[1]
            observed_labels.append(label)
            # PSD/MC-DC CSVs use labels such as ``T||F => T`` and
            # ``組合せ(F||F => F①)`` for additional vectors under the same
            # branch.  They carry an outcome but are not new branch headers.
            compact = re.sub(r"\s+", "", label).upper()
            if label.startswith("TRUE") or ("=>T" in compact) \
                    or "組合せ(TRUE" in label.upper():
                current_outcome = True
                current_vector_label = label
                if current_branch is not None:
                    golden_outcome_labels.setdefault(current_branch, []).append(label)
            elif label.startswith("FALSE") or ("=>F" in compact) \
                    or "組合せ(FALSE" in label.upper():
                current_outcome = False
                current_vector_label = label
                if current_branch is not None:
                    golden_outcome_labels.setdefault(current_branch, []).append(label)
            elif label.lstrip().startswith("組合せ("):
                # WinAMS emits repeated vectors for one switch case/branch as
                # ``組合せ(case ...:(n))``.  They are additional rows under
                # the current branch, not new branch headers.
                # Keep the exact combination label.  The parent case/default
                # remains in ``case_label`` for semantics, while the vector
                # description must retain the Golden's visible label.
                current_vector_label = label
                if current_branch is not None:
                    golden_outcome_labels.setdefault(current_branch, []).append(label)
                continue
            elif re.match(r"^(?:case\b|default\b)", label.strip(), re.IGNORECASE):
                # A case is a child of the current switch; do not consume a
                # FunctionIR branch index needed by following if statements.
                current_case = label.strip()
                current_outcome = None
                current_vector_label = label
                continue
            else:
                branch_index += 1
                current_branch = branch_index
                current_outcome = None
                current_case = None
                current_vector_label = None
                branch_labels.append(label)
            continue
        if not row or row[0] != "" or len(row) <= 1:
            continue
        cells = [_value(item) for item in row[1:1 + input_count + output_count]]
        raw_cells = row[1:1 + input_count + output_count]
        inputs = dict(zip(comments[:input_count], cells[:input_count]))
        expected = dict(zip(
            comments[input_count:input_count + output_count], cells[input_count:]
        ))
        scenarios.append({
            "case_id": f"U{len(scenarios) + 1:03d}",
            "branch_index": current_branch,
            "outcome": current_outcome,
            "kind": "case" if current_case else "scenario",
            "case_label": current_case,
            "label": current_vector_label,
            "inputs": inputs,
            "expected": expected,
            "raw_inputs": dict(zip(comments[:input_count], raw_cells[:input_count])),
            "raw_expected": dict(zip(
                comments[input_count:input_count + output_count],
                raw_cells[input_count:input_count + output_count],
            )),
            "value_classes": {
                key: _value_class(value) for key, value in {**inputs, **expected}.items()
            },
        })
    bindings: dict[str, str] = {}
    # 当条件变量是局部临时值（例如寄存器读取结果）时，利用人工用例中已经
    # 标注的 TRUE/FALSE 关系，在所有数值输入列中寻找唯一能证明该原子的来源。
    # 这里只生成待审批证据，不在正常生成阶段读取 golden。
    from ut_agent.rules.engine import evaluate_atom

    for branch_index, branch in enumerate(ir.branches):
        relevant = [
            item for item in scenarios
            if item.get("branch_index") == branch_index
            and item.get("outcome") is not None
        ]
        for atom in branch.atoms:
            identifiers = re.findall(r"[A-Za-z_]\w*", atom.var)
            if not identifiers or not relevant:
                continue
            variable = identifiers[0]
            if all(variable in item["inputs"] for item in relevant):
                continue
            matches = []
            related_columns = [
                column for column in comments[:input_count]
                if "AMIN_return" in column and _column_related(variable, column)
            ]
            if len(related_columns) == 1 and len(identifiers) == 1:
                # A uniquely named AMIN_return column is a structural stub
                # binding.  Other atoms in the same compound branch may make
                # outcome-only inference impossible, so do not require a
                # single-variable truth proof here; keep it as candidate
                # evidence for approval.
                column = related_columns[0]
                bindings[variable] = column
                for item in scenarios:
                    if column in item["inputs"]:
                        item["inputs"][variable] = item["inputs"][column]
                continue
            columns = related_columns if len(related_columns) == 1 else comments[:input_count]
            for column in columns:
                if not (_column_related(variable, column)
                        or atom.mask is not None
                        or bool(re.match(r"^\s*[A-Za-z_]\w*\s*&", atom.var or ""))
                        or bool(re.match(r"^\s*[A-Za-z_]\w*\s*&", atom.text or ""))):
                    continue
                try:
                    if all(
                        evaluate_atom(atom, {variable: item["inputs"][column]})
                        == bool(item["outcome"])
                        for item in relevant
                    ):
                        matches.append(column)
                except (KeyError, TypeError, ValueError):
                    continue
            if len(matches) == 1:
                bindings[variable] = matches[0]
                for item in scenarios:
                    if matches[0] in item["inputs"]:
                        item["inputs"][variable] = item["inputs"][matches[0]]
    bindings.update(_bind_expression_columns(ir, comments[:input_count], scenarios))
    bindings.update(_bind_indexed_input_columns(ir, comments[:input_count], scenarios))
    bindings.update(_bind_config_table_columns(ir, scenarios))
    bindings.update(_bind_comparison_operands(ir, comments[:input_count], scenarios))
    bindings.update(_bind_stub_call_columns(ir, comments[:input_count], scenarios))
    derived_bindings = _bind_derived_local_columns(ir, comments[:input_count], scenarios)
    output_bindings = _bind_output_columns(
        ir, comments[input_count:input_count + output_count], scenarios
    )
    digest = hashlib.sha256(data).hexdigest()
    input_value_classes: dict[str, set[str]] = {name: set() for name in comments[:input_count]}
    for item in scenarios:
        for name, value in item["inputs"].items():
            if name in input_value_classes:
                input_value_classes[name].add(_value_class(value))
    rule_evidence = {
        "observed_label_count": len(observed_labels),
        "mcdc_combinations": sorted({
            label for label in observed_labels
            if "=>" in re.sub(r"\s+", "", label)
            or label.lstrip().startswith("組合せ(TRUE")
            or label.lstrip().startswith("組合せ(FALSE")
        }),
        "case_labels": sorted({
            label.strip() for label in observed_labels
            if re.match(r"^(?:case\b|default\b)", label.strip(), re.IGNORECASE)
        }),
        "input_value_classes": {
            name: sorted(values) for name, values in input_value_classes.items()
        },
    }
    return {
        "name": f"{ir.name}-inferred",
        "version": 1,
        "rules": [{
            "id": f"project.{ir.name}.scenarios",
            "status": "candidate",
            "scope": {"function": ir.name},
            "match": {"kind": "scenario_matrix"},
            "action": {
                "input_columns": comments[:input_count],
                "output_columns": comments[input_count:input_count + output_count],
                "bindings": bindings,
                "derived_bindings": derived_bindings,
                "output_bindings": output_bindings,
                "golden_branch_labels": branch_labels,
                "golden_outcome_labels": [
                    golden_outcome_labels.get(index, [])
                    for index in range(len(branch_labels))
                ],
                "stub_declarations": stub_declarations,
                "rule_evidence": rule_evidence,
                "scenarios": scenarios,
            },
            "priority": 10,
            "evidence": [f"sha256:{digest}", str(golden)],
            "approval": {},
        }],
    }
