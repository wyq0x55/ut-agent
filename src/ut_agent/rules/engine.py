"""确定性测试意图生成、约束求值和验证门禁。"""
from __future__ import annotations

from itertools import product
from pathlib import Path
import re
from typing import Any

from ut_agent.cases.boundary import control_candidates, domain_of
from ut_agent.ir import Atom, Branch, FunctionIR, selected_global_writes
from ut_agent.rules.model import (
    Constraint, GenerationResult, NEEDS_REVIEW, RuleTrace, TestIntent,
    TestObligation, UNSUPPORTED, VALIDATED, ValidationResult,
)
from ut_agent.rules.pack import BUILTIN_PACK, Rule, RulePack


_BITMASK = re.compile(
    r"(?P<var>[A-Za-z_]\w*)\s*&\s*(?:\([^)]*\))*\s*(?P<mask>0[xX][0-9A-Fa-f]+|\d+)[uUlL]*"
)
_MEMORY_HELPER = re.compile(
    r".*(?:read|orwrite|andwrite|xorwrite|write).*?reg(?:8|16|32|64)$",
    re.IGNORECASE,
)


def _stub_calls(ir: FunctionIR) -> list:
    """Return the first source occurrence for each visible stub callee."""
    calls = []
    seen: set[str] = set()
    for call in sorted(ir.calls, key=lambda item: item.order):
        callee = (call.callee or "").strip()
        if (not callee or callee in seen
                or _MEMORY_HELPER.fullmatch(callee) or call.ptr_call):
            continue
        seen.add(callee)
        calls.append(call)
    return calls


def _stub_capacity(ir: FunctionIR, call) -> int:
    """Mirror the renderer's deterministic visible-slot calculation."""
    if getattr(call, "via_macro", None):
        return 1
    occurrences = [
        item for item in ir.calls
        if (item.callee or "").strip() == (call.callee or "").strip()
        and not _MEMORY_HELPER.fullmatch(item.callee or "")
        and not item.ptr_call
    ]
    total = 0
    for item in occurrences:
        try:
            total += max(1, int((item.extensions or {}).get("call_capacity", 1)))
        except (TypeError, ValueError):
            total += 1
    return max(1, total)


def _stub_param_fields(call, index: int) -> list[str]:
    """Read the same caller-side field metadata used by CSV rendering."""
    extensions = getattr(call, "extensions", {}) or {}
    metadata = extensions.get(
        "caller_param_fields", extensions.get("param_fields")
    )
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


def _stub_return_fields(call) -> list[str]:
    extensions = getattr(call, "extensions", {}) or {}
    raw = extensions.get("return_fields", ())
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).lstrip(".") for item in raw if str(item).lstrip(".")]


def _stub_input_columns(ir: FunctionIR) -> tuple[list[str], list[str]]:
    """Return exact WinAMS stub input and observable argument columns.

    Generic synthesis has no reference CSV to copy values from, but a
    validated row still has to contain every column promised by ``#COMMENT``.
    Keeping this small column model in the engine prevents the renderer from
    becoming an implicit source of testcase data and preserves the
    first-callee/call-capacity ordering contract.
    """
    param_columns: list[str] = []
    return_columns: list[str] = []
    for call in _stub_calls(ir):
        capacity = _stub_capacity(ir, call)
        extensions = getattr(call, "extensions", {}) or {}
        for index, param in enumerate(call.params):
            slot_name = (
                f"PTROUT{index:02d}_{call.callee}"
                if param.is_ptr else f"ARG{index:02d}_{call.callee}"
            )
            fields = _stub_param_fields(call, index) if param.is_ptr else []
            names = (
                [f"{slot_name}[{slot}].{field}"
                 for field in fields for slot in range(capacity)]
                if fields else
                [f"{slot_name}[{slot}]" for slot in range(capacity)]
            )
            qualified = [f"{call.callee}@{name}" for name in names]
            param_columns.extend(qualified)
            # The generic AST path treats a non-Rte stub argument as an
            # observable write-back slot.  Rte_Read receive objects are local
            # implementation state and remain input-only, matching the CSV
            # renderer's output-column rule.
            callee = str(call.callee or "")
            if not (callee.startswith("Rte_Read_") or (
                callee.startswith("pal_") and "_get_" in callee
            )):
                pointer_arguments = extensions.get("pointer_arguments", {})
                is_pal_set = callee.startswith("pal_") and "_set_" in callee
                info = pointer_arguments.get(str(index), {}) \
                    if isinstance(pointer_arguments, dict) else {}
                if is_pal_set and param.is_ptr and isinstance(info, dict) \
                        and info.get("is_address") and not info.get("is_null"):
                    # WinAMS exposes a PAL setter's address-passed byte as a
                    # second index (PTROUT[call][0]); this is distinct from
                    # the stub input slot PTROUT[call].
                    return_columns.extend(f"{column}[0]" for column in qualified)
                else:
                    return_columns.extend(qualified)
        return_used = extensions.get(
            "return_used", call.ret_type not in ("", "void")
        )
        if return_used:
            fields = _stub_return_fields(call)
            if fields:
                return_columns.extend(
                    f"{call.callee}@AMIN_return[{slot}].{field}"
                    for field in fields for slot in range(capacity)
                )
            else:
                return_columns.extend(
                    f"{call.callee}@AMIN_return[{slot}]"
                    for slot in range(capacity)
                )
    return param_columns, return_columns


def _global_input_columns(ir: FunctionIR) -> list[str]:
    """Expand AST global-object facts into exact WinAMS input spellings."""
    columns: list[str] = []
    objects = (ir.extensions or {}).get("global_objects", [])
    if not isinstance(objects, list):
        return columns
    for obj in objects:
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        if not (obj.get("read") or obj.get("write")):
            continue
        # Non-volatile const configuration is compile-time state, not a
        # controllable WinAMS variable.
        if obj.get("is_const") and not obj.get("is_volatile"):
            continue
        source_file = str(obj.get("source_file") or Path(ir.file).name)
        prefix = (
            "" if Path(source_file).suffix.lower() in {".h", ".hh", ".hpp", ".hxx"}
            else f"{Path(source_file).name}/"
        )
        base = f"{prefix}{obj['name']}"
        field_paths = [
            str(item).lstrip(".") for item in obj.get("field_paths", ())
            if str(item).lstrip(".")
        ]
        sizes: list[int] = []
        for raw_size in obj.get("array_sizes", ()):
            try:
                sizes.append(max(0, int(raw_size)))
            except (TypeError, ValueError):
                sizes = []
                break
        indexes = list(product(*(range(size) for size in sizes))) if sizes else [()]
        for index in indexes:
            indexed = base + "".join(f"[{item}]" for item in index)
            if field_paths:
                columns.extend(
                    f"{indexed}.{field}" for field in field_paths
                )
            else:
                columns.append(indexed)
    return columns


def _global_object_base(ir: FunctionIR, obj: dict[str, Any]) -> str:
    source_file = str(obj.get("source_file") or Path(ir.file).name)
    suffix = Path(source_file).suffix.lower()
    prefix = "" if suffix in {".h", ".hh", ".hpp", ".hxx"} \
        else f"{Path(source_file).name}/"
    if obj.get("is_const") and obj.get("is_volatile"):
        prefix = ""
    return f"{prefix}{obj['name']}"


def _global_output_columns(ir: FunctionIR) -> list[str]:
    """Expand only AST-proven global write columns like the CSV renderer."""
    columns: list[str] = []
    objects = (ir.extensions or {}).get("global_objects", [])
    if not isinstance(objects, list):
        return columns
    for obj in objects:
        if not isinstance(obj, dict) or not obj.get("name") or not obj.get("write"):
            continue
        if obj.get("is_const") and not obj.get("is_volatile"):
            continue
        base = _global_object_base(ir, obj)
        field_paths = [
            str(item).lstrip(".") for item in obj.get("field_paths", ())
            if str(item).lstrip(".")
        ]
        raw_accesses = (obj.get("extensions") or {}).get("field_accesses", [])
        accesses = {
            str(item.get("path", "")).lstrip("."): (
                bool(item.get("read")), bool(item.get("write"))
            )
            for item in raw_accesses if isinstance(item, dict)
            and item.get("path")
        } if isinstance(raw_accesses, list) else {}
        if field_paths and accesses:
            field_paths = [
                path for path in field_paths
                if any(
                    (read or write)
                    and (path == access or path.startswith(access + ".")
                         or access.startswith(path + "."))
                    for access, (read, write) in accesses.items()
                )
            ]
        sizes: list[int] = []
        for raw_size in obj.get("array_sizes", ()):
            try:
                sizes.append(max(0, int(raw_size)))
            except (TypeError, ValueError):
                sizes = []
                break
        indexes = list(product(*(range(size) for size in sizes))) if sizes else [()]
        if any(size == 0 for size in sizes):
            continue
        if field_paths:
            columns.extend(
                f"{base}{''.join(f'[{item}]' for item in index)}.{field}"
                for field in field_paths for index in indexes
            )
        else:
            columns.extend(
                f"{base}{''.join(f'[{item}]' for item in index)}"
                for index in indexes
            )
    return columns


def _global_effect_column(ir: FunctionIR, effect: dict[str, Any],
                          env: dict[str, Any]) -> str | None:
    path = _norm(str(effect.get("path", "")))
    match = re.fullmatch(
        r"([A-Za-z_]\w*)((?:\[[^\]]+\])*)(?:\.([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*))?",
        path,
    )
    if match is None:
        return None
    name, indexes_text, field = match.groups()
    objects = (ir.extensions or {}).get("global_objects", [])
    obj = next(
        (item for item in objects if isinstance(item, dict)
         and str(item.get("name")) == name), None,
    ) if isinstance(objects, list) else None
    if obj is None:
        return None
    indexes: list[int] = []
    for expression in re.findall(r"\[([^\]]+)\]", indexes_text):
        try:
            value = _lookup(env, expression)
        except KeyError:
            value = None
            try:
                value = int(expression, 0)
            except ValueError:
                pass
            if value is None:
                value = _local_value(ir, expression, env)
        if not isinstance(value, int):
            return None
        indexes.append(value)
    column = _global_object_base(ir, obj)
    column += "".join(f"[{index}]" for index in indexes)
    return f"{column}.{field}" if field else column


def _global_effect_value(ir: FunctionIR, effect: dict[str, Any],
                         env: dict[str, Any]) -> Any | None:
    constant = effect.get("constant_value")
    if constant is not None:
        return constant
    expression = str(effect.get("value", "")).strip()
    try:
        return _lookup(env, expression)
    except KeyError:
        value = _integer_expression_value(expression, env)
        if value is not None:
            return value
        origin = effect.get("origin")
        if not isinstance(origin, dict):
            return None
        if origin.get("kind") == "stub_return":
            return _stub_return_value(ir, origin, env)
        driver = str(origin.get("driver", ""))
        if driver:
            try:
                return _lookup(env, driver)
            except KeyError:
                try:
                    offset = int(effect.get("source_offset", -1))
                except (TypeError, ValueError):
                    offset = -1
                return _local_value(ir, driver, env, before_offset=offset)
    return None


def _integer_expression_value(expression: str,
                              env: dict[str, Any]) -> int | None:
    """Evaluate a small, AST-proven integer expression without ``eval``.

    Clang emits compound global writes such as ``value[index] & 0xdf`` as a
    single RHS expression.  The expression is still deterministic, but it is
    not a direct environment key.  Keep the evaluator deliberately narrow:
    C integer casts, literals, source variable paths, parentheses and the
    ordinary bitwise/arithmetic operators are supported; unknown syntax stays
    unresolved and therefore remains reviewable.
    """
    text = str(expression or "").strip()
    if not text:
        return None
    cast = re.compile(
        r"\(\s*(?:const\s+|volatile\s+|unsigned\s+|signed\s+)*"
        r"(?:u\d+|s\d+|b\d+|f\d+|d\d+|"
        r"uint\d*_t|int\d*_t|char|short|int|long|void)\s*\)"
    )
    previous = None
    while text != previous:
        previous = text
        text = cast.sub("", text)
    text = re.sub(r"(?<=\d)[uUlL]+\b", "", text)
    def normalize_index(match: re.Match[str]) -> str:
        index = _norm(match.group(1))
        while index.startswith("(") and index.endswith(")"):
            index = index[1:-1]
        return f"[{index}]"
    text = re.sub(r"\[([^\]]+)\]", normalize_index, text)
    token_re = re.compile(
        r"\s*(?:(0[xX][0-9A-Fa-f]+|[0-9]+)|"
        r"([A-Za-z_]\w*(?:\[[^\]]+\])?(?:\.|->)[A-Za-z_]\w*|"
        r"[A-Za-z_]\w*(?:\[[^\]]+\])?)|"
        r"(<<|>>|[|^&+\-*/%~()]))"
    )
    tokens: list[tuple[str, str]] = []
    offset = 0
    while offset < len(text):
        match = token_re.match(text, offset)
        if match is None:
            return None
        number, identifier, operator = match.groups()
        if number is not None:
            tokens.append(("number", number))
        elif identifier is not None:
            tokens.append(("identifier", _norm(identifier)))
        else:
            tokens.append(("operator", operator))
        offset = match.end()
    if not tokens:
        return None

    precedence = {
        "|": 1, "^": 2, "&": 3, "<<": 4, ">>": 4,
        "+": 5, "-": 5, "*": 6, "/": 6, "%": 6,
    }
    position = 0

    def primary() -> int | None:
        nonlocal position
        if position >= len(tokens):
            return None
        kind, value = tokens[position]
        if kind == "operator" and value in {"+", "-", "~"}:
            position += 1
            operand = primary()
            if operand is None:
                return None
            if value == "+":
                return operand
            if value == "-":
                return -operand
            return ~operand
        if kind == "operator" and value == "(":
            position += 1
            result = parse_expression(0)
            if (position >= len(tokens)
                    or tokens[position] != ("operator", ")")):
                return None
            position += 1
            return result
        if kind == "number":
            position += 1
            try:
                return int(value, 0)
            except ValueError:
                return None
        if kind == "identifier":
            position += 1
            try:
                resolved = _lookup(env, value)
            except KeyError:
                return None
            return resolved if isinstance(resolved, int) else None
        return None

    def parse_expression(minimum: int) -> int | None:
        nonlocal position
        left = primary()
        if left is None:
            return None
        while position < len(tokens):
            kind, operator = tokens[position]
            if kind != "operator" or operator not in precedence:
                break
            level = precedence[operator]
            if level < minimum:
                break
            position += 1
            right = parse_expression(level + 1)
            if right is None:
                return None
            try:
                if operator == "|":
                    left |= right
                elif operator == "^":
                    left ^= right
                elif operator == "&":
                    left &= right
                elif operator == "<<":
                    left <<= right
                elif operator == ">>":
                    left >>= right
                elif operator == "+":
                    left += right
                elif operator == "-":
                    left -= right
                elif operator == "*":
                    left *= right
                elif operator == "/":
                    left = int(left / right)
                elif operator == "%":
                    left %= right
            except (ArithmeticError, TypeError, ValueError):
                return None
        return left

    result = parse_expression(0)
    return result if position == len(tokens) else None


def _global_output_values(ir: FunctionIR, selected: dict[str, Any]) -> dict[str, Any] | None:
    columns = _global_output_columns(ir)
    if not columns:
        return {}
    env = _control_env(selected, ir)
    expected: dict[str, Any] = {}
    for column in columns:
        try:
            expected[column] = _lookup(selected, column)
        except KeyError:
            expected[column] = 0
    raw_effects = (ir.extensions or {}).get("global_write_effects", [])
    if not isinstance(raw_effects, list):
        return None
    touched: set[str] = set()
    unknown: set[str] = set()
    for effect in raw_effects:
        if not isinstance(effect, dict):
            continue
        if _guards_active(ir, effect.get("guards", []), env) is not True:
            continue
        column = _global_effect_column(ir, effect, env)
        root = _norm(str(effect.get("path", ""))).split("[", 1)[0].split(".", 1)[0]
        origin = effect.get("origin")
        # Do not let an alias in the testcase environment collapse a
        # whole-record automatic-local copy into a scalar.  The extractor
        # emits leaf effects for the local record; those leaves are the only
        # precise values for the expanded WinAMS columns.
        has_leaf_columns = bool(
            column and any(key.startswith(column + ".") for key in expected)
        )
        value = (
            None
            if (isinstance(origin, dict)
                    and origin.get("kind") == "local"
                    and has_leaf_columns)
            else _global_effect_value(ir, effect, env)
        )
        if column and isinstance(origin, dict) \
                and origin.get("kind") == "stub_return":
            # A structured return is assigned to a global/union member as a
            # single C expression, but WinAMS observes the returned record as
            # separate leaf columns.  Resolve those leaves from the exact
            # AMIN return slot instead of treating the aggregate as the
            # scalar value of the return slot.  The scalar slot is populated
            # with zero by generic input synthesis, so checking ``value is
            # None`` here would silently bypass the structured mapping.
            field_values = _stub_return_field_values(ir, origin, env)
            mapped = False
            for field, field_value in field_values.items():
                leaf = f"{column}.{field}"
                if leaf in expected:
                    expected[leaf] = field_value
                    mapped = True
            if mapped:
                # The two SW records in this project are eight one-bit
                # fields overlaid with ``byte``.  When all eight return
                # leaves are available, the byte output is a deterministic
                # AST type-layout consequence rather than a default value.
                if (column.endswith(".xng_sw_info")
                        or column.endswith(".xng_swic_pi_info")):
                    base = column.rsplit(".", 1)[0]
                    byte_column = f"{base}.byte"
                    ordered_fields = list(field_values)
                    if (byte_column in expected
                            and len(ordered_fields) == 8
                            and all(isinstance(field_values[item], int)
                                    for item in ordered_fields)):
                        expected[byte_column] = sum(
                            (int(field_values[item]) & 1) << index
                            for index, item in enumerate(ordered_fields)
                        )
                touched.add(root)
                continue
        if value is None and column and isinstance(origin, dict) \
                and origin.get("kind") == "local":
            # A whole-record copy from an automatic local is represented by
            # one Clang effect.  The local field effects carry the actual
            # leaf expressions, so replay them onto every statically sized
            # array element selected by the loop index.
            targets: list[tuple[str, dict[str, Any]]] = [(column, env)]
            path_match = re.fullmatch(
                r"[A-Za-z_]\w*\[([^\]]+)\].*",
                _norm(str(effect.get("path", ""))),
            )
            if path_match:
                index_expression = path_match.group(1)
                try:
                    index_value = int(index_expression, 0)
                except ValueError:
                    index_value = None
                if index_value is None:
                    obj = next(
                        (item for item in (ir.extensions or {}).get(
                            "global_objects", [])
                         if isinstance(item, dict)
                         and str(item.get("name")) == root),
                        None,
                    )
                    sizes = (obj or {}).get("array_sizes", [])
                    if isinstance(sizes, list) and len(sizes) == 1:
                        try:
                            limit = max(0, int(sizes[0]))
                        except (TypeError, ValueError):
                            limit = 0
                        if limit:
                            targets = []
                            open_bracket = column.find("[")
                            close_bracket = column.find("]", open_bracket)
                            for index in range(limit):
                                target = (
                                    column[:open_bracket + 1]
                                    + str(index) + column[close_bracket:]
                                )
                                target_env = dict(env)
                                target_env[index_expression] = index
                                target_env[_norm(index_expression)] = index
                                targets.append((target, target_env))
            mapped = False
            driver = str(origin.get("driver", ""))
            try:
                source_offset = int(effect.get("source_offset", -1))
            except (TypeError, ValueError):
                source_offset = -1
            for target, target_env in targets:
                prefix = target + "."
                for leaf in expected:
                    if not leaf.startswith(prefix):
                        continue
                    field = leaf[len(prefix):]
                    field_value = _local_field_value(
                        ir, driver, field, target_env,
                        before_offset=source_offset,
                    )
                    if field_value is None:
                        continue
                    expected[leaf] = field_value
                    mapped = True
            if mapped:
                touched.add(root)
                continue
        if column is None or value is None or column not in expected:
            if root:
                unknown.add(root)
            continue
        expected[column] = value
        touched.add(root)
    if not raw_effects and any(
        isinstance(obj, dict) and obj.get("write")
        for obj in (ir.extensions or {}).get("global_objects", [])
    ):
        return None
    for name in touched:
        expected[name] = next(
            (value for column, value in expected.items()
             if column.split("/")[-1].split("[", 1)[0] == name),
            0,
        )
    for name in unknown:
        for column in list(expected):
            tail = column.split("/")[-1]
            if tail == name or tail.startswith(name + "[") or tail.startswith(name + "."):
                expected.pop(column, None)
        expected.pop(name, None)
    objects = (ir.extensions or {}).get("global_objects", [])
    if isinstance(objects, list):
        for obj in objects:
            if isinstance(obj, dict) and obj.get("write") and obj.get("name"):
                # Validation uses the semantic object name for completeness;
                # rendering consumes the fully expanded field columns above.
                expected.setdefault(str(obj["name"]), 0)
    return expected


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _lookup(env: dict[str, Any], name: str) -> Any:
    compact = _norm(name)
    aliases = [compact, compact.lstrip("@*"), compact.split("/")[-1]]
    for alias in reversed(aliases):
        for candidate in (alias, alias.rstrip("]")):
            if candidate in env:
                return env[candidate]
    raise KeyError(name)


def _expanded_env(values: dict[str, Any]) -> dict[str, Any]:
    """给 WinAMS 全限定列名建立只读语义别名。"""
    env = dict(values)
    for key, value in values.items():
        compact = _norm(key)
        env.setdefault(compact, value)
        tail = compact.split("/")[-1]
        env.setdefault(tail, value)
        env.setdefault(tail.lstrip("@*"), value)
        if "@" in tail:
            env.setdefault(tail.rsplit("@", 1)[-1], value)
    return env


def _control_env(values: dict[str, Any], ir: FunctionIR) -> dict[str, Any]:
    """Add source-derived aliases for the shared branch evaluator.

    The C++ extractor records when an automatic control is produced by a
    const-table lookup or a stub return.  Those automatic names are semantic
    aliases only; the actual testcase value remains the parameter/table index
    or the WinAMS ``AMIN_return`` column.
    """
    env = _expanded_env(values)
    for control in ir.control_vars:
        value = None
        try:
            value = _lookup(env, control.name)
        except KeyError:
            origin = control.extensions.get("value_origin", {})
            if isinstance(origin, dict):
                kind = origin.get("kind")
                if kind == "stub_return":
                    callee = str(origin.get("callee", ""))
                    order = origin.get("call_order")
                    try:
                        order = int(order)
                    except (TypeError, ValueError):
                        order = None
                    aliases = []
                    if callee:
                        # A local assigned from a call is evaluated against
                        # the first slot at that call site.  Repeated calls
                        # retain call_order in IR and are handled by the
                        # renderer's capacity expansion; the control fact is
                        # still bound to its source callee here.
                        slot = _stub_return_slot(
                            ir, callee, order,
                            control.extensions.get("value_origin", {}).get(
                                "call_offset"
                            ),
                        )
                        aliases.append(
                            f"AMSTB_SrcFile.c/AMSTB_{callee}@AMIN_return[{slot}]"
                        )
                        aliases.append(f"AMSTB_{callee}@AMIN_return[{slot}]")
                        aliases.append(f"AMIN_return[{slot}]")
                    for alias in aliases:
                        try:
                            value = _lookup(env, alias)
                            break
                        except KeyError:
                            continue
                elif kind == "const_table_field":
                    driver = str(origin.get("driver", ""))
                    table_values = origin.get("table_values", {})
                    if driver and isinstance(table_values, dict):
                        try:
                            index = int(_lookup(env, driver))
                            value = table_values.get(str(index))
                            if value is None:
                                raise KeyError(control.name)
                        except (KeyError, TypeError, ValueError):
                            value = None
                    else:
                        value = None
                    if value is None:
                        continue
            else:
                continue
            if value is None:
                # Automatic locals are not WinAMS IO.  If Clang recorded the
                # local's assignment chain, resolve its value at the control
                # expression instead of promoting the local to an input.
                # The source offset prevents a later branch assignment from
                # being used to prove an earlier condition.
                if control.source == "local":
                    provenance = getattr(control, "provenance", None)
                    expansion = getattr(provenance, "expansion", None)
                    offset = getattr(expansion, "offset", None)
                    try:
                        offset = int(offset) if offset is not None else None
                    except (TypeError, ValueError):
                        offset = None
                    value = _local_value(
                        ir, control.name, env, before_offset=offset,
                    )
                if value is None:
                    continue
        env.setdefault(_norm(control.var), value)
        env.setdefault(control.name, value)
        origin = control.extensions.get("value_origin", {})
        if isinstance(origin, dict) and origin.get("kind") == "stub_return":
            callee = str(origin.get("callee", ""))
            if callee:
                # The local control candidate is the semantic value selected
                # by the solver.  Reflect it back to the concrete WinAMS
                # return slot; leaving the generic fixed zero here would make
                # the branch proof correct only through the local alias while
                # the rendered AMIN_return column contained a different value.
                origin = control.extensions.get("value_origin", {})
                slot = _stub_return_slot(
                    ir, callee, origin.get("call_order"),
                    origin.get("call_offset"),
                )
                concrete = (
                    f"AMSTB_SrcFile.c/AMSTB_{callee}"
                    f"@AMIN_return[{slot}]"
                )
                env[concrete] = value
                env[f"AMSTB_{callee}@AMIN_return[{slot}]"] = value
                env[f"AMIN_return[{slot}]"] = value
    return env


def evaluate_atom(atom: Atom, env: dict[str, Any],
                  post_env: dict[str, Any] | None = None) -> bool:
    """对受支持的整数原子条件求值；未知表达式显式抛出。"""
    left: Any
    # The extractor records the variable side of a NULL guard as ``var``
    # even when the source spelling is ``NULL != ptr``.  Treat the macro as
    # the integer null value instead of parsing the variable as both sides.
    if atom.boundary_name == "NULL" and atom.op in {"==", "!="}:
        left = _lookup(env, atom.var)
        right = 0
        return bool(left == right) if atom.op == "==" else bool(left != right)
    match = _BITMASK.search(atom.var) or _BITMASK.search(atom.text)
    if match:
        mask = atom.mask if atom.mask is not None else int(match.group("mask"), 0)
        left = int(_lookup(env, match.group("var"))) & mask
    elif atom.mask is not None:
        identifier = re.match(r"[A-Za-z_]\w*", atom.var)
        if identifier is None:
            raise ValueError(f"位掩码变量不可解析: {atom.var}")
        left = int(_lookup(env, identifier.group(0))) & atom.mask
    else:
        left = _lookup(env, atom.var)
    if atom.boundary is None:
        # Source-derived variable-to-variable comparisons (typically a static
        # table field versus an indexed error counter) remain deterministic
        # when both operands were bound from the same WinAMS scenario.
        dynamic = re.search(r"(?P<op>==|!=|<=|>=|<|>)\s*(?P<rhs>[^<>=]+?)\s*$", atom.text)
        if dynamic is None:
            raise ValueError(f"原子条件没有可求值边界: {atom.text}")
        rhs = dynamic.group("rhs").strip().strip("()")
        # A dynamic comparison may read a state variable updated by a stub
        # call earlier in the function.  When a Golden oracle supplies the
        # post-call value, use it for the RHS while keeping the left/config
        # operand from the input environment.  Fall back to the initial state
        # for expressions without an output alias.
        if post_env is not None:
            try:
                right = _lookup(post_env, rhs)
            except KeyError:
                right = _lookup(env, rhs)
        else:
            right = _lookup(env, rhs)
        op = dynamic.group("op")
    else:
        right = atom.boundary
        op = atom.op
    operations = {
        "==": lambda: left == right, "!=": lambda: left != right,
        "<": lambda: left < right, "<=": lambda: left <= right,
        ">": lambda: left > right, ">=": lambda: left >= right,
    }
    if op not in operations:
        raise ValueError(f"不支持的比较操作: {op}")
    return bool(operations[op]())


def _evaluate_condition_tree(tree: Any, atoms: list[Atom],
                             env: dict[str, Any],
                             post_env: dict[str, Any] | None = None) -> bool:
    if not isinstance(tree, dict):
        raise ValueError("分支 condition_tree 格式错误")
    kind = tree.get("kind")
    if kind == "atom":
        try:
            index = int(tree["index"])
            atom = atoms[index]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("condition_tree 原子索引错误") from exc
        return evaluate_atom(atom, env, post_env)
    if kind == "logical":
        children = tree.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError("condition_tree logical 节点没有子节点")
        values = [
            _evaluate_condition_tree(child, atoms, env, post_env)
            for child in children
        ]
        if tree.get("op") == "&&":
            return all(values)
        if tree.get("op") == "||":
            return any(values)
        raise ValueError(f"condition_tree 不支持连接词: {tree.get('op')}")
    raise ValueError(f"condition_tree 不支持节点: {kind}")


def evaluate_branch(branch: Branch, env: dict[str, Any],
                    post_env: dict[str, Any] | None = None) -> bool:
    if branch.constant_value is not None:
        return branch.constant_value
    tree = (branch.extensions or {}).get("condition_tree")
    if tree is not None:
        return _evaluate_condition_tree(tree, branch.atoms, env, post_env)
    if not branch.atoms:
        raise ValueError(f"分支 {branch.bid} 没有原子条件")
    values = [evaluate_atom(atom, env, post_env) for atom in branch.atoms]
    if len(values) == 1:
        return values[0]
    if branch.connective == "&&":
        return all(values)
    if branch.connective == "||":
        return any(values)
    raise ValueError(f"分支 {branch.bid} 的混合连接词尚不支持")


def _type_ok(value: Any, var_type: str | None, enums: dict) -> bool:
    if not isinstance(value, (int, float)):
        return True
    dom = domain_of(var_type, enums)
    if dom is None:
        return True
    if dom[0] == "set":
        return value in dom[1]
    return dom[1] <= value <= dom[2]


def _required_outputs(ir: FunctionIR) -> list[str]:
    required = []
    if ir.ret_type not in ("", "void"):
        required.append("ret")
    required.extend(param.name for param in ir.params if param.is_ptr and param.is_written)
    required.extend(memory.name for memory in ir.memory_vars if memory.write)
    required.extend(selected_global_writes(ir))
    # A dead branch can make ``selected_global_writes`` intentionally empty,
    # while WinAMS still exposes the AST global object on the output side of
    # #COMMENT.  Keep validation aligned with the rendered contract so such a
    # row becomes NEEDS_REVIEW before rendering rather than failing on a
    # missing oracle.
    global_objects = (ir.extensions or {}).get("global_objects", [])
    if isinstance(global_objects, list):
        required.extend(
            str(obj["name"]) for obj in global_objects
            if isinstance(obj, dict) and obj.get("name") and obj.get("write")
        )
    return list(dict.fromkeys(required))


def _has_key(values: dict[str, Any], wanted: str) -> bool:
    compact = _norm(wanted)
    if compact == "ret" and any(_norm(key).endswith("@@") for key in values):
        return True
    return any(
        _norm(key) == compact or _norm(key).endswith("/" + compact)
        or compact.endswith("/" + _norm(key))
        or _norm(key).lstrip("@*") == compact.lstrip("@*")
        for key in values
    )


def _loop_only_local_controls(ir: FunctionIR) -> set[str]:
    """Return local induction variables, not external testcase controls.

    A ``for`` condition is still retained as a branch header for WinAMS, but
    its iterator is assigned by the function itself.  Treating it as an
    input makes the generic solver fail before it can solve the real global
    or parameter controls in the function.  The fallback atom/branch match
    keeps this compatible with older IRs that did not serialize branch_ids.
    """
    branch_by_id = {branch.bid: branch for branch in ir.branches}
    result: set[str] = set()
    for control in ir.control_vars:
        if control.source != "local":
            continue
        related = [branch_by_id[bid] for bid in control.branch_ids
                   if bid in branch_by_id]
        if not related:
            related = [
                branch for branch in ir.branches if branch.kind == "for"
                and any(_norm(atom.var) == _norm(control.var)
                        for atom in branch.atoms)
            ]
        if any(branch.kind == "for" for branch in related):
            result.add(control.name)
    return result


def _remap_derived_candidates(ir: FunctionIR, candidates: dict) -> None:
    """Move const-table branch values onto the controllable index.

    A local such as ``table[index].field`` is not a WinAMS column.  When the
    extractor supplied constant initializer facts, invert the finite table
    relation so the input domain contains ``index`` values instead.
    """
    by_name = {cv.name: cv for cv in ir.control_vars}
    by_var = {_norm(cv.var): cv for cv in ir.control_vars}
    for control in ir.control_vars:
        origin = control.extensions.get("value_origin", {})
        if not isinstance(origin, dict) or origin.get("kind") != "const_table_field":
            continue
        table_values = origin.get("table_values", {})
        driver_name = str(origin.get("driver", ""))
        driver = by_name.get(driver_name) or by_var.get(_norm(driver_name))
        source = candidates.get(control.name)
        if driver is None or not source or not isinstance(table_values, dict):
            candidates.pop(control.name, None)
            continue
        desired = set(source.get("values", set()))
        indexes: set[int] = set()
        for raw_index, raw_value in table_values.items():
            try:
                index = int(raw_index)
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if not desired or value in desired:
                indexes.add(index)
        if indexes:
            target = candidates.setdefault(
                driver.name,
                {"cv": driver, "values": set(), "enum": {}},
            )
            target["values"].update(indexes)
        candidates.pop(control.name, None)


def _pointer_initial_value(selected: dict[str, Any], param) -> Any | None:
    """Read the caller-owned pointee value when the AST exposed a read path."""
    name = str(param.name)
    for key in (f"*{name}", f"@{name}[0]", f"{name}[0]", name):
        try:
            return _lookup(selected, key)
        except KeyError:
            continue
    return None


def _write_effect_value(ir: FunctionIR, effect: dict[str, Any],
                        env: dict[str, Any]) -> Any | None:
    constant = effect.get("constant_value")
    if constant is not None:
        return constant
    expression = str(effect.get("value", "")).strip()
    if not expression:
        return None
    try:
        return _lookup(env, expression)
    except KeyError:
        match = re.fullmatch(r"\(?\s*([A-Za-z_]\w*)\s*\)?", expression)
        if match:
            return _local_value(ir, match.group(1), env)
        return None


def _guards_active(ir: FunctionIR, guards: Any, env: dict[str, Any]) -> bool | None:
    """Evaluate an extractor guard list without treating unknown as false."""
    if not isinstance(guards, list):
        return True
    for guard in guards:
        if not isinstance(guard, dict):
            return None
        branch = next(
            (item for item in ir.branches if item.bid == guard.get("bid")), None
        )
        if branch is None:
            return None
        try:
            active = evaluate_branch(branch, env)
        except (KeyError, TypeError, ValueError):
            return None
        if active != bool(guard.get("then")):
            return False
    return True


def _stub_return_slot(ir: FunctionIR, callee: str, call_order: Any,
                      call_offset: Any = None) -> int:
    """Map a Clang call order/offset to the visible WinAMS return slot."""
    try:
        target_order = int(call_order)
    except (TypeError, ValueError):
        target_order = None
    if target_order is None:
        try:
            target_offset = int(call_offset)
        except (TypeError, ValueError):
            target_offset = None
        if target_offset is not None:
            for call in ir.calls:
                if (call.callee or "") != callee:
                    continue
                provenance = getattr(call, "provenance", None)
                points = (
                    getattr(provenance, "spelling", None),
                    getattr(provenance, "expansion", None),
                )
                offsets = {
                    getattr(point, "offset", None) for point in points
                }
                if target_offset in offsets:
                    target_order = call.order
                    break
    # An origin without an order/offset is an incomplete extractor fact.  The
    # conservative fallback is the first visible slot, never the last slot
    # of a repeated callee.
    if target_order is None:
        target_order = min(
            (call.order for call in ir.calls
             if (call.callee or "") == callee),
            default=0,
        )
    slot = 0
    for call in sorted(ir.calls, key=lambda item: item.order):
        if (call.callee or "") != callee:
            continue
        if call.order >= target_order:
            break
        slot += _stub_capacity(ir, call)
    return slot


def _stub_return_value(ir: FunctionIR, origin: dict[str, Any],
                       env: dict[str, Any]) -> Any | None:
    callee = str(origin.get("callee", ""))
    if not callee:
        return None
    slot = _stub_return_slot(
        ir, callee, origin.get("call_order"), origin.get("call_offset")
    )
    candidates = (
        f"AMSTB_SrcFile.c/AMSTB_{callee}@AMIN_return[{slot}]",
        f"AMSTB_{callee}@AMIN_return[{slot}]",
        f"{callee}@AMIN_return[{slot}]",
        f"AMIN_return[{slot}]",
    )
    for candidate in candidates:
        try:
            return _lookup(env, candidate)
        except KeyError:
            continue
    return None


def _stub_param_value(ir: FunctionIR, origin: dict[str, Any],
                      env: dict[str, Any]) -> Any | None:
    """Resolve a local written through a visible stub pointer argument."""
    callee = str(origin.get("callee", ""))
    if not callee:
        return None
    try:
        index = int(origin.get("index"))
    except (TypeError, ValueError):
        return None
    try:
        call_order = int(origin.get("call_order"))
    except (TypeError, ValueError):
        call_order = None
    call = next((item for item in ir.calls
                 if (item.callee or "") == callee
                 and (call_order is None or item.order == call_order)), None)
    if call is None and call_order is None:
        try:
            call_offset = int(origin.get("call_offset"))
        except (TypeError, ValueError):
            call_offset = None
        if call_offset is not None:
            def offset_of(value: object, location: str) -> int | None:
                provenance = getattr(value, "provenance", None)
                point = getattr(provenance, location, None)
                offset = getattr(point, "offset", None)
                try:
                    return int(offset) if offset is not None else None
                except (TypeError, ValueError):
                    return None
            call = next(
                (item for item in ir.calls
                 if (item.callee or "") == callee
                 and call_offset in {
                     offset_of(item, "spelling"),
                     offset_of(item, "expansion"),
                 }),
                None,
            )
    if call is None:
        return None
    slot = 0
    for item in sorted(ir.calls, key=lambda value: value.order):
        if item.order >= call.order:
            break
        if (item.callee or "") == callee:
            slot += _stub_capacity(ir, item)
    name = f"PTROUT{index:02d}_{callee}[{slot}]"
    candidates = (
        f"AMSTB_SrcFile.c/AMSTB_{callee}@{name}",
        f"AMSTB_{callee}@{name}",
        f"{callee}@{name}",
    )
    for candidate in candidates:
        try:
            return _lookup(env, candidate)
        except KeyError:
            continue
    return None


def _stub_return_field_values(ir: FunctionIR, origin: dict[str, Any],
                              env: dict[str, Any]) -> dict[str, Any]:
    """Resolve the visible fields of one structured stub return slot.

    Clang records a whole-record assignment as one global write effect, while
    WinAMS exposes the return object field-by-field.  Keep that conversion in
    the engine so a proven ``global[local_index].member = stub_return`` write
    can populate the same leaf columns that the renderer emits.
    """
    callee = str(origin.get("callee", ""))
    if not callee:
        return {}
    try:
        call_order = int(origin.get("call_order"))
    except (TypeError, ValueError):
        call_order = None
    call = next(
        (item for item in ir.calls
         if (item.callee or "") == callee
         and (call_order is None or item.order == call_order)),
        None,
    )
    if call is None:
        return {}
    fields = _stub_return_fields(call)
    if not fields:
        return {}
    slot = _stub_return_slot(ir, callee, call.order)
    values: dict[str, Any] = {}
    for field in fields:
        candidates = (
            f"AMSTB_SrcFile.c/AMSTB_{callee}@AMIN_return[{slot}].{field}",
            f"AMSTB_{callee}@AMIN_return[{slot}].{field}",
            f"{callee}@AMIN_return[{slot}].{field}",
            f"AMIN_return[{slot}].{field}",
        )
        for candidate in candidates:
            try:
                values[field] = _lookup(env, candidate)
                break
            except KeyError:
                continue
    return values


def _local_value_effects(ir: FunctionIR) -> list[dict[str, Any]]:
    raw = (ir.extensions or {}).get("local_value_effects", [])
    return [item for item in raw if isinstance(item, dict)] \
        if isinstance(raw, list) else []


def _origin_value(ir: FunctionIR, origin: Any, env: dict[str, Any],
                  seen: set[str]) -> Any | None:
    if not isinstance(origin, dict):
        return None
    kind = str(origin.get("kind", ""))
    if kind == "stub_return":
        return _stub_return_value(ir, origin, env)
    if kind == "stub_param":
        return _stub_param_value(ir, origin, env)
    if kind == "const_table_field":
        driver = str(origin.get("driver", ""))
        values = origin.get("table_values", {})
        if driver and isinstance(values, dict):
            try:
                try:
                    index = _lookup(env, driver)
                except KeyError:
                    index = _local_value(ir, driver, env)
                if index is None:
                    return None
                return values.get(str(int(index)))
            except (KeyError, TypeError, ValueError):
                return None
    driver = str(origin.get("driver", ""))
    if not driver:
        return None
    try:
        return _lookup(env, driver)
    except KeyError:
        if driver in seen:
            return None
        # _local_value adds the requested name to the recursion set itself.
        # Passing ``seen | {driver}`` pre-marked the first upstream local and
        # made a valid one-step assignment chain appear unresolved.
        return _local_value(ir, driver, env, seen, None)


def _local_value(ir: FunctionIR, name: str, env: dict[str, Any],
                 seen: set[str] | None = None,
                 before_offset: int | None = None) -> Any | None:
    """Resolve a local through the AST-recorded assignment chain."""
    seen = set() if seen is None else seen
    if name in seen:
        return None
    seen.add(name)
    effects = [
        item for item in _local_value_effects(ir)
        if _norm(str(item.get("name", ""))) == _norm(name)
    ]
    if before_offset is not None:
        bounded = []
        for item in effects:
            try:
                offset = int(item.get("source_offset", -1))
            except (TypeError, ValueError):
                offset = -1
            if offset < 0 or offset <= before_offset:
                bounded.append(item)
        effects = bounded
    effects.sort(key=lambda item: int(item.get("source_offset", -1)))
    for effect in reversed(effects):
        active = _guards_active(ir, effect.get("guards", []), env)
        if active is not True:
            continue
        constant = effect.get("constant_value")
        expression = str(effect.get("expression", "")).strip()
        if constant is not None:
            value = constant
        else:
            try:
                value = _lookup(env, expression)
            except KeyError:
                value = _origin_value(
                    ir, effect.get("origin"), env, set(seen),
                )
                if value is None and expression:
                    value = _integer_expression_value(expression, env)
        if value is None:
            continue
        operation = str(effect.get("operator", "="))
        if operation == "=":
            return value
        try:
            offset = int(effect.get("source_offset", -1))
        except (TypeError, ValueError):
            offset = -1
        previous = _local_value(
            ir, name, env, seen - {name},
            before_offset=offset - 1 if offset >= 0 else None,
        )
        if previous is None:
            continue
        try:
            lhs, rhs = int(previous), int(value)
            if operation == "|=":
                return lhs | rhs
            if operation == "&=":
                return lhs & rhs
            if operation == "^=":
                return lhs ^ rhs
            if operation == "+=":
                return lhs + rhs
            if operation == "-=":
                return lhs - rhs
            if operation == "*=":
                return lhs * rhs
            if operation == "/=" and rhs != 0:
                return int(lhs / rhs)
            if operation == "%=" and rhs != 0:
                return lhs % rhs
            if operation == "<<=":
                return lhs << rhs
            if operation == ">>=":
                return lhs >> rhs
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return None


def _local_field_value(ir: FunctionIR, name: str, path: str,
                       env: dict[str, Any],
                       before_offset: int | None = None) -> Any | None:
    """Resolve one AST-recorded field assignment of an automatic record."""
    effects = [
        item for item in _local_value_effects(ir)
        if _norm(str(item.get("name", ""))) == _norm(name)
        and _norm(str(item.get("path", ""))) == _norm(path)
    ]
    if before_offset is not None:
        bounded = []
        for item in effects:
            try:
                offset = int(item.get("source_offset", -1))
            except (TypeError, ValueError):
                offset = -1
            if offset < 0 or offset <= before_offset:
                bounded.append(item)
        effects = bounded
    effects.sort(key=lambda item: int(item.get("source_offset", -1)))
    for effect in reversed(effects):
        if _guards_active(ir, effect.get("guards", []), env) is not True:
            continue
        constant = effect.get("constant_value")
        if constant is not None:
            return constant
        expression = str(effect.get("expression", "")).strip()
        try:
            return _lookup(env, expression)
        except KeyError:
            value = _origin_value(ir, effect.get("origin"), env, set())
            if value is not None:
                return value
    return None


def _return_value(ir: FunctionIR, selected: dict[str, Any]) -> Any | None:
    """Prove the tested-function return for the selected AST path."""
    raw = (ir.extensions or {}).get("return_effects", [])
    if not isinstance(raw, list) or not raw:
        return None
    env = _control_env(selected, ir)
    applicable: list[Any] = []
    for effect in raw:
        if not isinstance(effect, dict):
            continue
        if _guards_active(ir, effect.get("guards", []), env) is not True:
            continue
        constant = effect.get("constant_value")
        if constant is not None:
            value = constant
        else:
            expression = str(effect.get("value", "")).strip()
            try:
                value = _lookup(env, expression)
            except KeyError:
                origin = effect.get("origin")
                value = _origin_value(ir, origin, env, set())
                if value is None:
                    # A return of a local automatic object is only
                    # controllable through its AST assignment chain.
                    name_match = re.fullmatch(
                        r"\(?\s*([A-Za-z_]\w*)\s*\)?", expression
                    )
                    if name_match:
                        try:
                            offset = int(effect.get("source_offset", -1))
                        except (TypeError, ValueError):
                            offset = -1
                        value = _local_value(
                            ir, name_match.group(1), env, before_offset=offset,
                        )
            if value is None:
                return None
        applicable.append(value)
    if not applicable:
        return None
    return applicable[-1] if all(item == applicable[-1] for item in applicable) else None


def _pointer_output_columns(param, effects: list[dict[str, Any]]) -> list[str]:
    name = str(param.name)
    static_function = False
    columns: list[str] = []
    for effect in effects:
        path = str(effect.get("path", "")).strip()
        if not path or path == name:
            continue
        if path.startswith("*"):
            column = f"@{name}[0]" if static_function \
                and path == f"*{name}" else path
        elif path.startswith(name):
            column = f"@{path}"
        else:
            column = f"*{name}"
        if column not in columns:
            columns.append(column)
    return columns


def _pointer_output_values(ir: FunctionIR, param,
                           selected: dict[str, Any]) -> dict[str, Any] | None:
    """Prove each caller-visible pointer output path from AST effects."""
    extensions = param.extensions or {}
    effects = extensions.get("write_effects", [])
    if not isinstance(effects, list) or not effects:
        return None
    env = _control_env(selected, ir)
    output_effects = [effect for effect in effects if isinstance(effect, dict)
                      and effect.get("path")]
    columns = _pointer_output_columns(param, output_effects)
    values: dict[str, Any] = {column: 0 for column in columns}
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        guards = effect.get("guards", [])
        known = True
        for guard in guards if isinstance(guards, list) else []:
            if not isinstance(guard, dict):
                known = False
                break
            branch = next(
                (item for item in ir.branches if item.bid == guard.get("bid")),
                None,
            )
            if branch is None:
                known = False
                break
            try:
                if evaluate_branch(branch, env) != bool(guard.get("then")):
                    known = False
                    break
            except (KeyError, TypeError, ValueError):
                known = False
                break
        if not known:
            continue
        value = _write_effect_value(ir, effect, env)
        if value is None:
            return None
        path = str(effect.get("path", "")).strip()
        if path:
            name = str(param.name)
            if path == name:
                continue
            if path.startswith("*"):
                column = path
            elif path.startswith(name):
                column = f"@{path}"
            else:
                column = f"*{name}"
            values[column] = value
    return values


def _pointer_output_value(ir: FunctionIR, param,
                          selected: dict[str, Any]) -> Any | None:
    """Return the last proven pointer value for legacy semantic aliases."""
    values = _pointer_output_values(ir, param, selected)
    if values is None:
        return None
    if values:
        return next(reversed(values.values()))
    return _pointer_initial_value(selected, param)


def _generic_expected(ir: FunctionIR, selected: dict[str, Any]) -> dict[str, Any]:
    expected = {
        memory.name: memory.expected_value for memory in ir.memory_vars
        if memory.write and memory.expected_value is not None
    }
    if ir.ret_type not in ("", "void"):
        value = _return_value(ir, selected)
        if value is not None:
            expected["ret"] = value
            static_function = bool(
                (ir.extensions or {}).get("is_static_function", False)
            )
            return_comment = (
                f"{Path(ir.file).name}/{ir.name}@@"
                if static_function else f"{ir.name}@@"
            )
            expected[return_comment] = value
    global_values = _global_output_values(ir, selected)
    if global_values is not None:
        expected.update(global_values)
    # CALLCNT is emitted as a comparison column on both sides of #COMMENT.
    # Generic synthesis stores the semantic short name, so materialize the
    # exact output spelling as well.
    for call in _stub_calls(ir):
        name = f"CALLCNT_{call.callee}"
        try:
            value = _lookup(selected, name)
        except KeyError:
            continue
        expected[name] = value
        expected[f"AMSTB_SrcFile.c/AMSTB_{call.callee}@{name}"] = value
    for param in ir.params:
        if not param.is_ptr or not param.is_written:
            continue
        pointer_values = _pointer_output_values(ir, param, selected)
        if pointer_values is not None:
            expected.update(pointer_values)
        value = _pointer_output_value(ir, param, selected)
        if value is not None:
            expected[param.name] = value
            # The renderer uses the exact pointee spelling in #COMMENT while
            # the solver traditionally stores the semantic parameter name.
            # Keep both forms so a proven pointer write is renderable for
            # static (``@p[0]``) and external (``*p``) targets.
            expected[f"@{param.name}[0]"] = value
            expected[f"{param.name}[0]"] = value
            expected[f"*{param.name}"] = value

    # Stub argument write-back columns are observable WinAMS outputs for the
    # ordinary non-Rte helpers.  Their deterministic generic oracle is the
    # selected input slot; callee side effects remain represented separately
    # by AST-proven tested-function pointer/global write effects.
    _, stub_output_columns = _stub_input_columns(ir)
    for column in stub_output_columns:
        if "@AMIN_return[" in column:
            continue
        try:
            canonical = f"AMSTB_SrcFile.c/AMSTB_{column}"
            expected[canonical] = _lookup(selected, canonical)
        except KeyError:
            # The corresponding exact input key is installed by
            # ``_generic_inputs``.  Keeping this guard explicit makes an
            # incomplete legacy IR remain NEEDS_REVIEW instead of silently
            # inventing an output value.
            continue
    return expected


def validate_intent(ir: FunctionIR, intent: TestIntent) -> ValidationResult:
    errors: list[str] = []
    checks: list[str] = []
    env = _control_env(intent.inputs, ir)
    for param in ir.params:
        if not _has_key(intent.inputs, param.name) and not _has_key(intent.inputs, "@" + param.name):
            errors.append(f"缺少函数参数输入: {param.name}")
    for memory in ir.memory_vars:
        if not _has_key(intent.inputs, memory.name):
            errors.append(f"缺少寄存器初值: {memory.name}")
    # stub 调用列属于输入契约，但其调用次数/参数只有场景规则或执行证据
    # 能确定；通用边界求解器不得伪造这些值。
    for call in ir.calls:
        if _MEMORY_HELPER.fullmatch(call.callee or "") or call.ptr_call:
            continue
        if not any(f"CALLCNT_{call.callee}" in str(key) for key in intent.inputs):
            errors.append(f"缺少 stub 调用次数证据: {call.callee}")
    for cv in ir.control_vars:
        if cv.constant_value is not None:
            env.setdefault(cv.name, cv.constant_value)
            env.setdefault(_norm(cv.var), cv.constant_value)
        try:
            # Arrays/structure members can produce duplicate short names in
            # FunctionIR (for example state[3] and state[4]). Prefer the full
            # source expression when the scenario rule supplied that alias;
            # fall back to the short name for ordinary scalar controls.
            try:
                value = _lookup(env, cv.var)
            except KeyError:
                value = _lookup(env, cv.name)
        except KeyError:
            if cv.source in ("param", "global", "local_from_global", "stub"):
                errors.append(f"缺少控制变量输入: {cv.name}")
            continue
        if not _type_ok(value, cv.var_type, ir.enums):
            errors.append(f"控制变量越界: {cv.name}={value}")
    checks.append("input-domain")

    obligation = intent.obligation
    if obligation.branch_id is not None and obligation.kind == "case":
        branch = next((item for item in ir.branches
                       if item.bid == obligation.branch_id), None)
        if branch is None:
            errors.append(f"不存在目标分支: {obligation.branch_id}")
        elif branch.kind != "switch":
            errors.append(f"case 目标不是 switch: {branch.bid}")
        else:
            case = _find_switch_case(branch, obligation)
            if case is None:
                errors.append(
                    f"不存在目标 case: {branch.bid}/{obligation.description}"
                )
            else:
                try:
                    actual = _switch_selector_value(branch, ir, env)
                    if not _switch_case_matches(case, actual, branch.cases):
                        errors.append(
                            f"case 结果不符: {branch.bid} 期望="
                            f"{_case_obligation_label(case)} 实际={actual}"
                        )
                    else:
                        checks.append("case-outcome")
                except (KeyError, TypeError, ValueError) as exc:
                    errors.append(f"case 不可证明: {branch.bid}: {exc}")
    elif obligation.branch_id is not None and obligation.outcome is not None:
        branch = next((item for item in ir.branches if item.bid == obligation.branch_id), None)
        if branch is None:
            errors.append(f"不存在目标分支: {obligation.branch_id}")
        else:
            try:
                # The Golden row records the observable post-call state.  For
                # variable-vs-variable comparisons, that state is needed when
                # an earlier stub call updates the RHS before this branch.
                actual = evaluate_branch(branch, env, _expanded_env(intent.expected))
                if actual != obligation.outcome:
                    errors.append(
                        f"分支结果不符: {branch.bid} 期望={obligation.outcome} 实际={actual}"
                    )
                else:
                    checks.append("branch-outcome")
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"分支不可证明: {branch.bid}: {exc}")

    for name in _required_outputs(ir):
        if not _has_key(intent.expected, name):
            errors.append(f"缺少期望值 oracle: {name}")
    checks.append("oracle-completeness")
    status = VALIDATED if not errors else NEEDS_REVIEW
    return ValidationResult(status, tuple(checks), tuple(errors))


def _case_obligation_label(case) -> str:
    """Return the same stable label that the CSV renderer uses for a case."""
    if case.is_default:
        return "default:"
    if case.value is not None:
        return f"case {case.value}:"
    return f"case {case.label}:"


def _find_switch_case(branch: Branch, obligation: TestObligation):
    wanted = (obligation.case_label or obligation.description or "").strip()
    for case in branch.cases:
        if wanted in {case.label, _case_obligation_label(case)}:
            return case
        if case.is_default and wanted.lower().startswith("default"):
            return case
        if case.value is not None:
            literals = re.findall(
                r"(?<![A-Za-z0-9_])(?:0[xX][0-9A-Fa-f]+|\d+)", wanted,
            )
            if any(
                (int(literal, 16) if literal.lower().startswith("0x")
                 else int(literal, 10)) == case.value
                for literal in literals
            ):
                return case
    return None


def _switch_selector_value(branch: Branch, ir: FunctionIR,
                           env: dict[str, Any]) -> Any:
    """Resolve a switch selector from the proof environment only.

    The selector may be an automatic loop/local variable.  It is still a
    useful proof value, but it must not become a rendered WinAMS input column.
    """
    condition = str(branch.cond_text or "").strip()
    if condition.startswith("switch(") and condition.endswith(")"):
        condition = condition[len("switch("):-1].strip()
    try:
        return _lookup(env, condition)
    except KeyError:
        normalized = _norm(condition)
        for control in ir.control_vars:
            if normalized not in {_norm(control.var), _norm(control.name)}:
                continue
            return _lookup(env, control.name)
    raise KeyError(condition)


def _switch_case_matches(case, value: Any, cases: list | None = None) -> bool:
    if case.is_default:
        explicit = {
            item.value for item in (cases or [])
            if not item.is_default and item.value is not None
        }
        return value not in explicit
    return case.value is not None and value == case.value


def _scenario_intents(ir: FunctionIR, rule: Rule) -> list[TestIntent]:
    scenarios = rule.action.get("scenarios", [])
    out: list[TestIntent] = []
    for index, raw in enumerate(scenarios, 1):
        branch_id = raw.get("branch_id")
        if branch_id is None and raw.get("branch_index") is not None:
            pos = int(raw["branch_index"])
            branch_id = ir.branches[pos].bid if 0 <= pos < len(ir.branches) else None
        outcome = raw.get("outcome")
        obligation = TestObligation(
            oid=str(raw.get("obligation_id", f"{rule.rule_id}:{index:03d}")),
            kind=str(raw.get("kind", "scenario")), branch_id=branch_id,
            outcome=None if outcome is None else bool(outcome),
            boundary_class=raw.get("boundary_class"),
            description=str(raw.get(
                "description", raw.get("label", raw.get("case_label", "approved scenario"))
            )),
            case_label=raw.get("case_label"),
        )
        scenario_inputs = dict(raw.get("inputs", {}))
        # A Golden scenario may intentionally omit helper calls that are not
        # part of its TestCsv I/O contract (for example hardware side-effect
        # helpers).  Keep those calls out of rendered columns, but bind a
        # deterministic zero count for validation so they are not mistaken
        # for missing evidence.  Generic synthesis still requires explicit
        # call-count evidence and is therefore unaffected.
        for call in ir.calls:
            if (_MEMORY_HELPER.fullmatch(call.callee or "") or call.ptr_call):
                continue
            scenario_inputs.setdefault(f"CALLCNT_{call.callee}", 0)
        intent = TestIntent(
            case_id=str(raw.get("case_id", f"U{index:03d}")),
            obligation=obligation,
            inputs=scenario_inputs, expected=dict(raw.get("expected", {})),
            raw_inputs=dict(raw.get("raw_inputs", {})),
            raw_expected=dict(raw.get("raw_expected", {})),
            stub_behavior=dict(raw.get("stub_behavior", {})),
            constraints=tuple(
                Constraint(**item) for item in raw.get("constraints", [])
            ),
            trace=(RuleTrace(rule.rule_id, "; ".join(rule.evidence),
                             "approved scenario matrix"),),
        )
        validation = validate_intent(ir, intent)
        out.append(TestIntent(**{**intent.__dict__, "validation": validation}))
    return out


def _generic_inputs(ir: FunctionIR) -> tuple[dict[str, list[Any]], dict[str, Any]]:
    candidates = control_candidates(ir)
    _remap_derived_candidates(ir, candidates)
    loop_locals = _loop_only_local_controls(ir)
    derivable_locals = {
        str(item.get("name"))
        for item in _local_value_effects(ir)
        if item.get("name") and not item.get("path")
    }
    allowed = {
        cv.name for cv in ir.control_vars
        if cv.constant_value is None
        and cv.source in ("param", "global", "local_from_global", "stub")
    }
    unresolved = [
        cv.name for cv in ir.control_vars
        if cv.constant_value is None
        and cv.name not in loop_locals
        and not (cv.source == "local" and cv.name in derivable_locals)
        and cv.source not in (
            "param", "global", "local_from_global", "stub", "derived"
        )
    ]
    if unresolved:
        raise ValueError(f"控制变量来源不可设定: {', '.join(sorted(unresolved))}")
    domains = {
        name: sorted(item["values"]) for name, item in candidates.items()
        # A pointer guard such as ``ptr != NULL`` has no finite boundary
        # candidates.  Its value is supplied by the builtin pointer rule;
        # keeping an empty domain here would make the whole Cartesian product
        # empty and discard otherwise provable dereferenced-value cases.
        # Loop iterators are not testcase inputs, but a switch nested under a
        # loop is exercised by a concrete iteration value.  Keep those values
        # in the proof environment only; the renderer excludes local controls
        # from the WinAMS input columns.
        if (name in allowed or name in loop_locals) and item["values"]
    }
    fixed: dict[str, Any] = {}
    for cv in ir.control_vars:
        if cv.constant_value is not None:
            fixed[cv.name] = cv.constant_value
    pointer_rule = BUILTIN_PACK.approved(ir.name, "pointer")[0]
    base = int(pointer_rule.action["base"])
    stride = int(pointer_rule.action["stride"])
    pointer_index = 0
    for param in ir.params:
        if not param.is_ptr:
            # A scalar formal is controllable even when it only drives an
            # array/table access and never appears in a branch predicate.
            # Materialize it here so validation and CSV rendering cannot
            # mistake an omitted parameter for an unresolved local.
            fixed.setdefault(param.name, 0)
            continue
        fixed[param.name] = base + pointer_index * stride
        pointer_index += 1
        # Address columns and dereferenced value columns are distinct
        # WinAMS variables.  A generic row starts with a deterministic zero
        # pointee; AST write effects may replace it in the expected half.
        fixed[f"@{param.name}[0]"] = 0
        fixed[f"{param.name}[0]"] = 0
        fixed[f"*{param.name}"] = 0
    for memory in ir.memory_vars:
        if memory.input_value is not None:
            fixed[memory.name] = memory.input_value
    # The CSV renderer expands structure/union globals from the same Clang
    # field-shape facts.  Materialize every possible AST-proven input spelling
    # so a validated generic row never relies on a renderer default.
    for name in _global_input_columns(ir):
        fixed.setdefault(name, 0)
    for name in ir.globals_used:
        fixed.setdefault(name, 0)
    known_call_counts: dict[str, int] = {}
    for call in ir.calls:
        if _MEMORY_HELPER.fullmatch(call.callee or "") or call.ptr_call:
            continue
        capacity = (call.extensions or {}).get("call_capacity")
        if isinstance(capacity, int) and capacity >= 0:
            known_call_counts[call.callee] = (
                known_call_counts.get(call.callee, 0) + capacity
            )
    for callee, count in known_call_counts.items():
        fixed[f"CALLCNT_{callee}"] = count
    stub_input_columns, stub_return_columns = _stub_input_columns(ir)
    for column in (*stub_input_columns, *stub_return_columns):
        fixed[f"AMSTB_SrcFile.c/AMSTB_{column}"] = 0
    return domains, fixed


def _generic_intents(
    ir: FunctionIR,
    semantic_rules: tuple[Rule, ...] = (),
) -> list[TestIntent]:
    domains, fixed = _generic_inputs(ir)
    keys = sorted(domains)
    values = [domains[key] for key in keys]
    combination_count = 1
    for domain in values:
        combination_count *= len(domain)
    combinations = list(product(*values)) if combination_count <= 4096 else None
    obligations: list[TestObligation] = []
    if ir.branches:
        for branch in ir.branches:
            # The iterator of a for-loop is internal state.  Preserve the
            # branch in CSV, but do not invent input vectors for it.
            if branch.kind == "for":
                continue
            if branch.kind == "switch" and branch.cases:
                # A switch has no boolean outcome of its own in TestCsv.  Its
                # executable obligations are the source cases; nested ifs
                # remain attached to the corresponding case in the renderer.
                for case_index, case in enumerate(branch.cases):
                    label = _case_obligation_label(case)
                    obligations.append(TestObligation(
                        oid=f"{branch.bid}:case:{case_index}",
                        kind="case", branch_id=branch.bid,
                        description=label, case_label=label,
                    ))
                continue
            outcomes = ((branch.constant_value,) if branch.constant_value is not None
                        else (True, False))
            for outcome in outcomes:
                obligations.append(TestObligation(
                    oid=f"{branch.bid}:{'T' if outcome else 'F'}",
                    kind="branch", branch_id=branch.bid, outcome=outcome,
                    description=branch.cond_text,
                ))
        # A function containing only internal loop headers still has a
        # deterministic executable entry obligation.  Do not expose loop
        # counters as testcase inputs just to manufacture a branch row.
        if not obligations:
            obligations.append(TestObligation(
                "ENTRY", "execution", description="function entry"
            ))
    else:
        obligations.append(TestObligation("ENTRY", "execution", description="function entry"))

    intents: list[TestIntent] = []
    used: set[tuple[tuple[str, Any], ...]] = set()
    for obligation in obligations:
        selected: dict[str, Any] | None = None
        candidate_envs = (
            (
                _control_env({**fixed, **dict(zip(keys, combo))}, ir)
                for combo in combinations
            )
            if combinations is not None else
            _targeted_generic_candidates(ir, domains, fixed, obligation)
        )
        for env in candidate_envs:
            if obligation.branch_id is None:
                selected = env
                break
            branch = next(item for item in ir.branches if item.bid == obligation.branch_id)
            try:
                if obligation.kind == "case":
                    case = _find_switch_case(branch, obligation)
                    if case is not None:
                        selector = _switch_selector_value(branch, ir, env)
                        if _switch_case_matches(case, selector, branch.cases):
                            selected = env
                            break
                elif evaluate_branch(branch, env) == obligation.outcome:
                    selected = env
                    break
            except (KeyError, TypeError, ValueError):
                continue
        if selected is None:
            continue
        signature = tuple(sorted(selected.items()))
        if signature in used:
            continue
        used.add(signature)
        expected = _generic_expected(ir, selected)
        branch = (next((item for item in ir.branches
                        if item.bid == obligation.branch_id), None)
                  if obligation.branch_id else None)
        constraints = tuple(
            Constraint("predicate", atom.var, atom.op, atom.boundary, atom.text)
            for atom in (branch.atoms if branch else [])
        ) + tuple(
            Constraint("pointer", param.name, "valid", selected[param.name],
                       "builtin.pointer")
            for param in ir.params if param.is_ptr and param.name in selected
        )
        trace_items = [
            RuleTrace("builtin.compare", "FunctionIR", "finite candidate proof")
        ]
        # A promoted semantic-family rule is an input to synthesis, not a
        # post-generation annotation.  Its normalized signature selects the
        # AST instantiation strategy; the trace makes that execution decision
        # auditable in the manifest.
        if semantic_rules:
            family_ids = _semantic_family_ids(ir)
            trace_items.extend(
                RuleTrace(rule.rule_id, "; ".join(rule.evidence),
                          "approved semantic-family instantiation")
                for rule in semantic_rules
                if str(rule.match.get("family_id", "")) in family_ids
            )
        if branch and any(_BITMASK.search(atom.var) or _BITMASK.search(atom.text)
                          for atom in branch.atoms):
            trace_items.append(RuleTrace(
                "builtin.bitmask", "FunctionIR.Atom.text", "bitmask candidate proof"
            ))
        if any(param.is_ptr for param in ir.params):
            trace_items.append(RuleTrace(
                "builtin.pointer", "WinAMS address contract", "non-overlapping address"
            ))
        intent = TestIntent(
            case_id=f"U{len(intents) + 1:03d}", obligation=obligation,
            inputs=selected, expected=expected, constraints=constraints,
            trace=tuple(trace_items),
        )
        validation = validate_intent(ir, intent)
        intents.append(TestIntent(**{**intent.__dict__, "validation": validation}))
    return intents


def _domain_key_for(ir: FunctionIR, expression: str,
                    domains: dict[str, list[Any]]) -> str | None:
    wanted = _norm(expression)
    for control in ir.control_vars:
        if wanted in {_norm(control.name), _norm(control.var)}:
            if control.name in domains:
                return control.name
    for key in domains:
        if _norm(key) == wanted:
            return key
    return None


def _targeted_domain_values(ir: FunctionIR, branch: Branch,
                            key: str, domains: dict[str, list[Any]]) -> list[Any]:
    """Keep only values needed to prove this branch.

    Five-point domains are useful for ordinary boundary enumeration, but an
    N-atom predicate can make their Cartesian product unnecessarily large.
    For a targeted proof, retain every literal boundary and one deterministic
    value outside those boundaries.  This keeps equality disjunctions such as
    the NMD predicates finite without weakening the AST proof.
    """
    atoms = [
        atom for atom in branch.atoms
        if _domain_key_for(ir, atom.var, domains) == key
    ]
    boundaries = {
        atom.boundary for atom in atoms if atom.boundary is not None
    }
    values = [value for value in domains.get(key, []) if value in boundaries]
    values.extend(
        value for value in domains.get(key, [])
        if value not in boundaries and value not in values
    )
    return values or list(domains.get(key, []))


def _condition_target_atoms(tree: Any, outcome: bool) -> list[tuple[int, bool]]:
    """Select a sufficient set of AST condition leaves for one outcome."""
    if not isinstance(tree, dict):
        return []
    if tree.get("kind") == "atom":
        try:
            return [(int(tree["index"]), outcome)]
        except (KeyError, TypeError, ValueError):
            return []
    if tree.get("kind") != "logical":
        return []
    children = tree.get("children")
    if not isinstance(children, list) or not children:
        return []
    op = tree.get("op")
    if op == "&&":
        if outcome:
            return [
                item for child in children
                for item in _condition_target_atoms(child, True)
            ]
        return _condition_target_atoms(children[0], False)
    if op == "||":
        if outcome:
            return _condition_target_atoms(children[0], True)
        return [
            item for child in children
            for item in _condition_target_atoms(child, False)
        ]
    return []


def _targeted_branch_candidate(ir: FunctionIR,
                               domains: dict[str, list[Any]],
                               fixed: dict[str, Any], branch: Branch,
                               outcome: bool) -> dict[str, Any] | None:
    """Construct one deterministic input vector for a branch outcome."""
    raw = dict(fixed)
    branch_keys = {
        key for atom in branch.atoms
        if (key := _domain_key_for(ir, atom.var, domains)) is not None
    }
    for key in sorted(domains):
        values = _targeted_domain_values(ir, branch, key, domains) \
            if key in branch_keys else domains[key]
        if values:
            raw[key] = values[0]

    atoms = list(branch.atoms)
    tree = (branch.extensions or {}).get("condition_tree")
    if tree is not None:
        target_atoms = _condition_target_atoms(tree, outcome)
    else:
        connective = branch.connective or "single"
        if connective == "&&":
            desired = [True] * len(atoms) if outcome else [False]
        elif connective == "||":
            desired = [False] * len(atoms) if not outcome else [True]
        else:
            desired = [outcome]
        if connective == "&&" and not outcome:
            target_atoms = [(0, False)] if atoms else []
        elif connective == "||" and outcome:
            target_atoms = [(0, True)] if atoms else []
        else:
            target_atoms = list(enumerate(desired))

    for atom_index, expected in target_atoms:
        atom = atoms[atom_index]
        key = _domain_key_for(ir, atom.var, domains)
        if key is None:
            continue
        values = _targeted_domain_values(ir, branch, key, domains)
        chosen = None
        for value in values:
            trial = dict(raw)
            trial[key] = value
            env = _control_env(trial, ir)
            try:
                if evaluate_atom(atom, env) == expected:
                    chosen = value
                    break
            except (KeyError, TypeError, ValueError):
                continue
        if chosen is not None:
            raw[key] = chosen

    env = _control_env(raw, ir)
    try:
        return env if evaluate_branch(branch, env) == outcome else None
    except (KeyError, TypeError, ValueError):
        return None


def _targeted_generic_candidates(ir: FunctionIR,
                                  domains: dict[str, list[Any]],
                                  fixed: dict[str, Any],
                                  obligation: TestObligation):
    """Yield bounded proof candidates when the full product is too large."""
    raw = dict(fixed)
    for key in sorted(domains):
        if domains[key]:
            raw[key] = domains[key][0]
    if obligation.branch_id is None:
        yield _control_env(raw, ir)
        return
    branch = next(
        (item for item in ir.branches if item.bid == obligation.branch_id), None
    )
    if branch is None:
        return
    # Large products may use targeted construction only when the C++ Clang
    # extractor preserved the condition AST.  Hand-built/legacy IR without
    # that provenance must retain the original UNSUPPORTED gate instead of
    # guessing a flattened multi-atom expression.
    if not (branch.extensions or {}).get("condition_tree"):
        return
    if obligation.kind == "case":
        key = _domain_key_for(ir, branch.cond_text, domains)
        if key is None:
            yield _control_env(raw, ir)
            return
        for value in domains[key]:
            trial = dict(raw)
            trial[key] = value
            env = _control_env(trial, ir)
            try:
                case = _find_switch_case(branch, obligation)
                if case is not None and _switch_case_matches(
                    case, _switch_selector_value(branch, ir, env), branch.cases
                ):
                    yield env
                    return
            except (KeyError, TypeError, ValueError):
                continue
        return
    if obligation.outcome is not None:
        candidate = _targeted_branch_candidate(
            ir, domains, fixed, branch, obligation.outcome
        )
        if candidate is not None:
            yield candidate
            return
    # A mixed connective or an expression with multiple controls may not be
    # constructible by the simple proof above.  Search the reduced relevant
    # product only, with a hard deterministic cap.
    relevant = sorted({
        key for atom in branch.atoms
        if (key := _domain_key_for(ir, atom.var, domains)) is not None
    })
    reduced = [
        _targeted_domain_values(ir, branch, key, domains) for key in relevant
    ]
    scanned = 0
    for combo in product(*reduced) if reduced else [()]:
        if scanned >= 100000:
            break
        scanned += 1
        trial = dict(raw)
        trial.update(dict(zip(relevant, combo)))
        env = _control_env(trial, ir)
        try:
            if evaluate_branch(branch, env) == obligation.outcome:
                yield env
                return
        except (KeyError, TypeError, ValueError):
            continue


def _semantic_family_ids(ir: FunctionIR) -> set[str]:
    """Return normalized branch-family IDs used by promoted semantic rules."""
    from ut_agent.rules.compress import _branch_family, _digest

    result: set[str] = set()
    for branch in ir.branches:
        atoms = []
        for atom in branch.atoms:
            if atom.boundary_name:
                boundary_class = "enum-or-macro"
            elif atom.boundary == 0:
                boundary_class = "zero"
            elif atom.boundary == 1:
                boundary_class = "one"
            elif atom.boundary is None:
                boundary_class = "unknown"
            else:
                boundary_class = "literal"
            atoms.append({
                "op": atom.op,
                "boundary_class": boundary_class,
                "masked": atom.mask is not None,
                "mask_width": (int(atom.mask).bit_length()
                               if atom.mask is not None else None),
            })
        family = _branch_family({
            "kind": branch.kind,
            "connective": branch.connective or "single",
            "atoms": atoms,
        })
        result.add(f"family.{_digest(family)}")
    return result


def generate_intents(ir: FunctionIR, pack: RulePack | None = None) -> GenerationResult:
    pack = pack or BUILTIN_PACK
    scenario_rules = pack.approved(ir.name, "scenario_matrix")
    if len(scenario_rules) > 1 and scenario_rules[0].priority == scenario_rules[1].priority:
        return GenerationResult(ir.name, UNSUPPORTED, issues=("场景规则优先级冲突",),
                                rule_pack=pack.name)
    semantic_rules = pack.approved(ir.name, "semantic_family")
    try:
        intents = (_scenario_intents(ir, scenario_rules[0]) if scenario_rules
                   else _generic_intents(ir, semantic_rules))
    except ValueError as exc:
        return GenerationResult(ir.name, UNSUPPORTED, issues=(str(exc),),
                                rule_pack=pack.name)
    if not intents:
        return GenerationResult(ir.name, UNSUPPORTED, issues=("没有可证明的测试用例",),
                                rule_pack=pack.name)
    statuses = {item.validation.status for item in intents}
    status = VALIDATED if statuses == {VALIDATED} else NEEDS_REVIEW
    issues = tuple(
        error for item in intents for error in item.validation.errors
    )
    action = scenario_rules[0].action if scenario_rules else {}
    return GenerationResult(
        ir.name, status, tuple(intents), issues, pack.name,
        tuple(str(item) for item in action.get("input_columns", [])),
        tuple(str(item) for item in action.get("output_columns", [])),
        tuple(str(item) for item in action.get("golden_branch_labels", [])),
        tuple(
            tuple(str(label) for label in labels)
            for labels in action.get("golden_outcome_labels", [])
        ),
        tuple(tuple(str(cell) for cell in row)
              for row in action.get("stub_declarations", [])),
    )
