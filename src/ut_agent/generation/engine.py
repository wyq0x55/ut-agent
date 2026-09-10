"""确定性测试意图生成、约束求值和验证门禁。"""
from __future__ import annotations

from dataclasses import asdict
from itertools import product
from typing import Any

from ut_agent.generation.boundary import (
    control_candidates, switch_default_points, typed_boundary_points,
    typed_status_points,
)
from ut_agent.ir import Atom, Branch, FunctionIR, TypeInfo
from ut_agent.generation.model import (
    Constraint, GenerationResult, NEEDS_REVIEW, RuleTrace, TestIntent,
    TestObligation, UNSUPPORTED, VALIDATED, ValidationResult,
)
from ut_agent.generation.pack import BUILTIN_PACK, Rule, RulePack
from ut_agent.generation.semantic import (
    call_columns as _semantic_call_columns,
    call_count_key,
    call_param_key,
    call_param_keys,
    call_return_keys,
    call_capacity as _stub_capacity,
    global_base_key,
    global_input_columns as _global_input_columns,
    global_key,
    global_output_columns as _global_output_columns,
    index_driver_limit as _semantic_index_driver_limit,
    output_columns as _semantic_call_output_columns,
    param_fields as _semantic_call_param_fields,
    pointer_address_key,
    pointer_value_key,
    visible_calls as _stub_calls,
    return_fields as _stub_return_fields,
)


def _is_memory_helper(call) -> bool:
    return call.callee_kind == "memory_helper"


def _global_records(ir: FunctionIR) -> list[dict[str, Any]]:
    return [asdict(item) for item in ir.global_objects]


def _origin_record(origin: Any) -> dict[str, Any] | None:
    if origin is None:
        return None
    if isinstance(origin, dict):
        return origin
    return asdict(origin)


def _effect_records(effects: list[Any]) -> list[dict[str, Any]]:
    return [asdict(item) if not isinstance(item, dict) else item for item in effects]


def _split_access_path(path: str) -> tuple[str, list[str], str | None] | None:
    """Split an extractor-proven object path for projection lookup.

    This is a structural path projection, not a C-expression parser.  The
    extractor has already identified the object, indexes, and member path;
    malformed paths are left unresolved for review.
    """
    text = _norm(path)
    if not text:
        return None
    cursor = 0
    while cursor < len(text) and text[cursor] not in "[.":
        cursor += 1
    name = text[:cursor]
    if not name:
        return None
    indexes: list[str] = []
    while cursor < len(text) and text[cursor] == "[":
        close = text.find("]", cursor + 1)
        if close <= cursor + 1:
            return None
        indexes.append(text[cursor + 1:close])
        cursor = close + 1
    field: str | None = None
    if cursor < len(text):
        if text[cursor] != "." or cursor + 1 >= len(text):
            return None
        field = text[cursor + 1:]
        if any(char in "[]" for char in field):
            return None
    return name, indexes, field


def _global_effect_column(ir: FunctionIR, effect: dict[str, Any],
                          env: dict[str, Any]) -> str | None:
    path = _norm(str(effect.get("path", "")))
    parts = _split_access_path(path)
    if parts is None:
        return None
    name, index_expressions, field = parts
    objects = _global_records(ir)
    obj = next(
        (item for item in objects if isinstance(item, dict)
         and str(item.get("name")) == name), None,
    ) if isinstance(objects, list) else None
    if obj is None:
        return None
    indexes: list[int] = []
    for expression in index_expressions:
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
    return global_key(name, tuple(indexes), field)


def _global_effect_value(ir: FunctionIR, effect: dict[str, Any],
                         env: dict[str, Any]) -> Any | None:
    constant = effect.get("constant_value")
    if constant is not None:
        return constant
    return _effect_expression_value(ir, effect, env)


def _is_integer_literal(value: str) -> bool:
    try:
        int(value, 0)
    except (TypeError, ValueError):
        return False
    return True


def _effect_execution_environments(ir: FunctionIR,
                                   effect: dict[str, Any],
                                   env: dict[str, Any]
                                   ) -> tuple[dict[str, Any], ...]:
    parts = _split_access_path(str(effect.get("path", "")))
    if parts is None or not any(
            not _is_integer_literal(index) for index in parts[1]):
        return (env,)
    try:
        offset = int(effect.get("source_offset", -1))
    except (TypeError, ValueError):
        offset = -1
    if offset < 0:
        return (env,)
    loops: list[tuple[int, int, dict[str, Any]]] = []
    for branch in ir.branches:
        if branch.kind != "for":
            continue
        span = _source_span(branch)
        if span is None or not span[0] <= offset <= span[1]:
            continue
        extensions = branch.extensions
        raw_loop = (extensions["execution_loop"]
                    if isinstance(extensions, dict)
                    and "execution_loop" in extensions else None)
        if not isinstance(raw_loop, dict):
            continue
        driver = raw_loop.get("driver")
        if not isinstance(driver, str) or not driver.strip():
            return (env,)
        try:
            start = int(raw_loop["start"])
            step = int(raw_loop["step"])
            count = int(raw_loop["count"])
        except (KeyError, TypeError, ValueError):
            return (env,)
        if step == 0 or count <= 0:
            return (env,)
        loops.append((span[0], span[1], {
            "driver": driver, "start": start, "step": step,
            "count": count,
        }))
    if not loops:
        return (env,)
    loops.sort(key=lambda item: (item[0], -item[1]))
    values = [
        [
            (raw["driver"], raw["start"] + raw["step"] * index)
            for index in range(raw["count"])
        ]
        for _, _, raw in loops
    ]
    result: list[dict[str, Any]] = []
    for combination in product(*values):
        current = dict(env)
        for driver, value in combination:
            current[_norm(driver)] = value
            current[driver] = value
        result.append(current)
    return tuple(result)


def _resolve_record_storage_values(
    ir: FunctionIR,
    expected: dict[str, Any],
    unresolved: set[str],
    columns: list[str],
) -> None:
    """Resolve scalar storage from extractor-proven bit-field layout facts."""
    for obj in _global_records(ir):
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        layout = obj.get("record_layout")
        if not isinstance(layout, list) or not layout:
            continue
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
        for index in indexes:
            base = global_key(str(obj["name"]), tuple(index))
            fields = [item for item in layout if isinstance(item, dict)]
            for storage in fields:
                if storage.get("is_bitfield"):
                    continue
                storage_path = str(storage.get("path", ""))
                try:
                    storage_offset = int(storage["bit_offset"])
                    storage_width = int(storage["bit_width"])
                except (KeyError, TypeError, ValueError):
                    continue
                if storage_width <= 0:
                    continue
                storage_column = f"{base}.{storage_path}"
                if storage_column not in columns:
                    continue
                bitfields = []
                visible_bitfields = []
                for field in fields:
                    if not field.get("is_bitfield"):
                        continue
                    try:
                        bit_offset = int(field["bit_offset"])
                        bit_width = int(field["bit_width"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if (bit_width <= 0 or bit_offset < storage_offset
                            or bit_offset + bit_width > storage_offset + storage_width):
                        continue
                    field_column = f"{base}.{field.get('path', '')}"
                    if field_column not in columns:
                        continue
                    visible_bitfields.append((bit_offset, bit_width, field_column))
                    if field_column in unresolved:
                        bitfields = None
                        break
                    value = expected.get(field_column)
                    if not isinstance(value, int):
                        bitfields = None
                        break
                    bitfields.append((bit_offset - storage_offset, bit_width, value))
                if bitfields is None or not visible_bitfields:
                    continue

                initial = expected.get(storage_column)
                storage_known = storage_column not in unresolved and isinstance(initial, int)
                if not storage_known:
                    # A partial bit-field write cannot establish the untouched
                    # storage bits.  Only synthesize from zero when the
                    # extractor facts prove that the visible fields cover the
                    # complete storage unit.
                    covered = sorted(
                        (offset, offset + width)
                        for offset, width, _ in visible_bitfields
                    )
                    cursor = storage_offset
                    for start, end in covered:
                        if start != cursor:
                            break
                        cursor = end
                    if cursor != storage_offset + storage_width:
                        continue
                    initial = 0

                value = int(initial)
                for offset, width, raw_value in bitfields:
                    mask = ((1 << width) - 1) << offset
                    value = (value & ~mask) | (
                        (int(raw_value) & ((1 << width) - 1)) << offset
                    )
                expected[storage_column] = value
                unresolved.discard(storage_column)


def _global_output_values(ir: FunctionIR, selected: dict[str, Any]) -> dict[str, Any] | None:
    columns = _global_output_columns(ir)
    if not columns:
        return {}
    env = _control_env(selected, ir)
    expected: dict[str, Any] = {}
    unresolved_columns: set[str] = set()
    for column in columns:
        try:
            expected[column] = _lookup(selected, column)
        except KeyError:
            unresolved_columns.add(column)
    raw_effects = _effect_records(ir.global_write_effects)
    effect_contexts = [
        (effect, effect_env)
        for effect in raw_effects if isinstance(effect, dict)
        for effect_env in _effect_execution_environments(ir, effect, env)
    ]
    for effect, effect_env in effect_contexts:
        env = effect_env
        if _guards_active(
                ir, effect.get("guards", []), effect_env,
                effect.get("source_offset")) is not True:
            continue
        column = _global_effect_column(ir, effect, effect_env)
        root = _norm(str(effect.get("path", ""))).split("[", 1)[0].split(".", 1)[0]
        origin = effect.get("origin")
        # Do not let an alias in the testcase environment collapse a
        # whole-record automatic-local copy into a scalar.  The extractor
        # emits leaf effects for the local record; those leaves are the only
        # precise values for the expanded target columns.
        has_leaf_columns = bool(
            column and any(key.startswith(column + ".") for key in columns)
        )
        value = (
            None
            if (isinstance(origin, dict)
                    and origin.get("kind") == "local"
                    and has_leaf_columns)
            else _global_effect_value(ir, effect, effect_env)
        )
        if column and isinstance(origin, dict) \
                and origin.get("kind") == "stub_return":
            # A structured return is assigned to a global/union member as a
            # single C expression, but the target adapter observes the returned
            # record as separate leaf columns.  Resolve those leaves from the
            # exact return slot instead of treating the aggregate as the
            # scalar value of the return slot.  The scalar slot is populated
            # with zero by generic input synthesis, so checking ``value is
            # None`` here would silently bypass the structured mapping.
            field_values = _stub_return_field_values(ir, origin, effect_env)
            mapped = False
            for field, field_value in field_values.items():
                leaf = f"{column}.{field}"
                if leaf in columns:
                    expected[leaf] = field_value
                    unresolved_columns.discard(leaf)
                    mapped = True
            if mapped:
                continue
        if value is None and column and isinstance(origin, dict) \
                and origin.get("kind") == "local":
            # A whole-record copy from an automatic local is represented by
            # one Clang effect.  The local field effects carry the actual
            # leaf expressions, so replay them onto every statically sized
            # array element selected by the loop index.
            targets: list[tuple[str, dict[str, Any]]] = [(column, env)]
            path_parts = _split_access_path(str(effect.get("path", "")))
            if path_parts and path_parts[1]:
                index_expression = path_parts[1][0]
                try:
                    index_value = int(index_expression, 0)
                except ValueError:
                    index_value = None
                if index_value is None:
                    obj = next(
                        (item for item in _global_records(ir)
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
                for leaf in columns:
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
                    unresolved_columns.discard(leaf)
                    mapped = True
            if mapped:
                continue
        if column is None or value is None or column not in columns:
            if root:
                unresolved_columns.update(
                    item for item in columns
                    if item == global_base_key(root)
                    or item.startswith(global_base_key(root) + "[")
                    or item.startswith(global_base_key(root) + ".")
                )
            continue
        expected[column] = value
        unresolved_columns.discard(column)
    if not raw_effects:
        return None
    _resolve_record_storage_values(ir, expected, unresolved_columns, columns)
    if unresolved_columns:
        return None
    return expected


def _norm(value: str) -> str:
    return "".join(str(value or "").split())


def _lookup(env: dict[str, Any], name: str) -> Any:
    compact = _norm(name)
    aliases = [compact, compact.lstrip("@*"), compact.split("/")[-1]]
    # Exact typed paths take precedence over short/tail aliases.  Dynamic
    # array members can have both spaced and compact spellings in one
    # environment; resolving the tail first lets an unrelated fixed column
    # mask the selected member value.
    for alias in aliases:
        for candidate in (alias, alias.rstrip("]")):
            if candidate in env:
                return env[candidate]
    raise KeyError(name)


def _expression_tree(origin: Any) -> dict[str, Any] | None:
    record = _origin_record(origin)
    if record is None:
        return None
    tree = record.get("expression_tree")
    return tree if isinstance(tree, dict) else None


def _expression_reference_path(tree: Any) -> str | None:
    if not isinstance(tree, dict):
        return None
    kind = tree.get("kind")
    if kind == "reference":
        name = str(tree.get("name", "")).strip()
        return name or None
    if kind == "member":
        base = _expression_reference_path(tree.get("base"))
        field = str(tree.get("field", "")).strip()
        if base and field:
            return f"{base}.{field}"
    return None


def _cast_expression_value(value: Any, type_info: Any) -> Any | None:
    if not isinstance(type_info, dict):
        return value
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    if type_info.get("kind") not in {"integer", "enum", "bool"}:
        return result
    width = type_info.get("bit_width")
    if not isinstance(width, int) or width <= 0 or width > 64:
        return result
    mask = (1 << width) - 1
    result &= mask
    if type_info.get("signed") and result & (1 << (width - 1)):
        result -= 1 << width
    return result


def _eval_expression_tree(ir: FunctionIR, tree: Any,
                          env: dict[str, Any],
                          seen: set[str] | None = None) -> Any | None:
    """Evaluate extractor-owned typed value facts, never source spelling."""
    if not isinstance(tree, dict):
        return None
    seen = set() if seen is None else seen
    kind = str(tree.get("kind", ""))
    if kind == "constant":
        return tree.get("value")
    if kind == "reference":
        name = str(tree.get("name", "")).strip()
        if not name:
            return None
        try:
            return _lookup(env, name)
        except KeyError:
            if name in seen:
                return None
            return _local_value(ir, name, env, seen)
    if kind == "member":
        path = _expression_reference_path(tree)
        if path:
            try:
                return _lookup(env, path)
            except KeyError:
                pass
        base = _eval_expression_tree(ir, tree.get("base"), env, seen)
        if isinstance(base, dict):
            return base.get(str(tree.get("field", "")))
        return None
    if kind == "subscript":
        base_path = _expression_reference_path(tree.get("base"))
        index = _eval_expression_tree(ir, tree.get("index"), env, seen)
        if base_path is not None and index is not None:
            try:
                return _lookup(env, f"{base_path}[{int(index)}]")
            except (KeyError, TypeError, ValueError):
                pass
        return None
    if kind == "cast":
        value = _eval_expression_tree(ir, tree.get("operand"), env, seen)
        return None if value is None else _cast_expression_value(
            value, tree.get("type_info")
        )
    if kind == "unary":
        op = str(tree.get("op", ""))
        value = _eval_expression_tree(ir, tree.get("operand"), env, seen)
        if value is None:
            return None
        try:
            if op in {"*", "&", "+"}:
                return value
            if op == "-":
                return -int(value)
            if op == "~":
                return ~int(value)
            if op == "!":
                return int(not bool(value))
        except (TypeError, ValueError):
            return None
        return None
    if kind == "binary":
        op = str(tree.get("op", ""))
        lhs = _eval_expression_tree(ir, tree.get("lhs"), env, seen)
        rhs = _eval_expression_tree(ir, tree.get("rhs"), env, seen)
        if lhs is None or rhs is None:
            return None
        try:
            left, right = int(lhs), int(rhs)
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/" and right != 0:
                return int(left / right)
            if op == "%" and right != 0:
                return left % right
            if op == "<<":
                return left << right
            if op == ">>":
                return left >> right
            if op == "&":
                return left & right
            if op == "|":
                return left | right
            if op == "^":
                return left ^ right
            if op == "==":
                return int(left == right)
            if op == "!=":
                return int(left != right)
            if op == "<":
                return int(left < right)
            if op == "<=":
                return int(left <= right)
            if op == ">":
                return int(left > right)
            if op == ">=":
                return int(left >= right)
            if op == "&&":
                return int(bool(left) and bool(right))
            if op == "||":
                return int(bool(left) or bool(right))
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        return None
    if kind == "conditional":
        condition = _eval_expression_tree(ir, tree.get("condition"), env, seen)
        if condition is None:
            return None
        selected = tree.get("then") if bool(condition) else tree.get("else")
        return _eval_expression_tree(ir, selected, env, seen)
    return None


def _effect_expression_value(ir: FunctionIR, effect: dict[str, Any],
                             env: dict[str, Any],
                             seen: set[str] | None = None) -> Any | None:
    expression = str(effect.get("value", "")).strip()
    if expression:
        try:
            return _lookup(env, expression)
        except KeyError:
            pass
    tree = _expression_tree(effect.get("origin"))
    if tree is not None:
        value = _eval_expression_tree(ir, tree, env, seen)
        if value is not None:
            return value
    return _origin_value(ir, effect.get("origin"), env,
                         set() if seen is None else seen)


def _expanded_env(values: dict[str, Any]) -> dict[str, Any]:
    """建立全限定目标列名的只读语义别名。"""
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
    or the target adapter's return column.
    """
    env = _expanded_env(values)
    for control in ir.control_vars:
        origin = _origin_record(control.value_origin)
        if isinstance(origin, dict) and origin.get("kind") == "local_from_global":
            driver = str(origin.get("driver", "")).strip()
            if driver:
                try:
                    val = _lookup(env, driver)
                    env[_norm(control.var)] = val
                    env[control.name] = val
                except KeyError:
                    pass
    for control in ir.control_vars:
        value = None
        for key in (control.var, control.name):
            if not key:
                continue
            # Prefer the extractor's exact semantic path before consulting
            # normalized aliases.  Dynamic array-member paths can coexist
            # with their space-normalized spelling; a fixed global column
            # must not mask the candidate value selected for this control.
            if key in values:
                value = values[key]
                break
            try:
                value = _lookup(env, key)
                break
            except KeyError:
                continue
        origin = _origin_record(control.value_origin)
        if isinstance(origin, dict) and origin.get("kind") == "local_from_global":
            # The automatic local is only an alias for the external global
            # recorded by Clang.  Always resolve the alias from that driver
            # when it is present so a stale local value cannot hide the
            # actual testcase input selected by the solver.
            driver = str(origin.get("driver", ""))
            if driver:
                try:
                    value = _lookup(env, driver)
                except KeyError:
                    value = None
        if value is None:
            if origin is not None:
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
                            origin.get("call_offset"),
                        )
                    aliases.extend(call_return_keys(callee, slot))
                    for alias in aliases:
                        try:
                            value = _lookup(env, alias)
                            break
                        except KeyError:
                            continue
                elif kind == "stub_param":
                    # A local/field copied from an externally filled pointer
                    # argument is controlled by the call's typed parameter
                    # slot, not by the automatic local name.  Resolve the
                    # exact field before falling back to the local effect
                    # chain so structured PAL/RTE outputs remain distinct.
                    value = _stub_param_value(ir, origin, env)
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
                # Automatic locals are not target IO.  If Clang recorded the
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
        if (isinstance(origin, dict)
                and origin.get("kind") == "local_from_global"):
            # Replace any stale automatic-local spelling with the value
            # resolved from the external driver above.
            env[_norm(control.var)] = value
            env[control.name] = value
        elif control.var in values or control.name in values:
            # Preserve the exact extractor path selected by the testcase even
            # when _expanded_env already contains a normalized alias from a
            # different spelling of the same dynamic member.
            env[_norm(control.var)] = value
            env[control.name] = value
        else:
            env.setdefault(_norm(control.var), value)
            env.setdefault(control.name, value)
        origin = _origin_record(control.value_origin)
        if origin is not None and origin.get("kind") == "stub_return":
            callee = str(origin.get("callee", ""))
            if callee:
                # The local control candidate is the semantic value selected
                # by the solver.  Reflect it back to the concrete target
                # return slot; leaving the generic fixed zero here would make
                # the branch proof correct only through the local alias while
                # the rendered return column contained a different value.
                slot = _stub_return_slot(
                    ir, callee, origin.get("call_order"),
                    origin.get("call_offset"),
                )
                for concrete in call_return_keys(callee, slot):
                    env[concrete] = value

    for param in ir.params:
        if param.is_ptr:
            ptr_val = env.get(param.name)
            if ptr_val == 0:
                env[pointer_address_key(param.name)] = 0
                env[param.name] = 0
            elif ptr_val is not None and ptr_val != 0:
                env.setdefault(pointer_address_key(param.name), 1)

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
    if atom.mask is not None:
        left = int(_lookup(env, atom.var)) & atom.mask
    else:
        left = _lookup(env, atom.var)
    if atom.boundary is None:
        # Source-derived variable-to-variable comparisons (typically a static
        # table field versus an indexed error counter) remain deterministic
        # when both operands were bound from the same target scenario.
        rhs = atom.right
        if not rhs:
            raise ValueError(f"原子条件没有可求值边界: {atom.text}")
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
        op = atom.op
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
    if kind == "not":
        child = tree.get("child")
        if not isinstance(child, dict):
            raise ValueError("condition_tree not 节点没有 child")
        return not _evaluate_condition_tree(child, atoms, env, post_env)
    raise ValueError(f"condition_tree 不支持节点: {kind}")


def evaluate_branch(branch: Branch, env: dict[str, Any],
                    post_env: dict[str, Any] | None = None) -> bool:
    if branch.constant_value is not None:
        return branch.constant_value
    tree = branch.condition_tree
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


def branch_path_reachable(ir: FunctionIR, branch: Branch,
                          env: dict[str, Any]) -> bool | None:
    """Check extractor-proven enclosing branch conditions.

    ``parent_bid`` is a semantic nesting fact, not a renderer hint.  A
    child branch in a normal nested body is executable only when its parent
    is true.  An ``elseif`` child is the alternative arm of its preceding
    chain and therefore requires the enclosing condition to be false.  A
    switch is a structural parent whose child branch is reachable only
    from an extractor-proven containing case.

    ``None`` means the available FunctionIR cannot prove the path; callers
    must keep that obligation reviewable rather than treating it as false.
    """
    by_id = {item.bid: item for item in ir.branches}
    current = branch
    visited: set[str] = set()
    while current.parent_bid:
        if current.bid in visited:
            return None
        visited.add(current.bid)
        parent = by_id.get(current.parent_bid)
        if parent is None:
            return None
        if parent.kind == "switch":
            cases = _switch_cases_for_branch(parent, current)
            if not cases:
                return None
            try:
                selector = _switch_selector_value(parent, ir, env)
            except (KeyError, TypeError, ValueError):
                return None
            if not any(
                    _switch_case_matches(case, selector, parent.cases)
                    for case in cases):
                return False
            current = parent
            continue
        if current.parent_outcome is not None:
            required = bool(current.parent_outcome)
        else:
            required = not (
                current.kind == "elseif" and current.chain_index > 0
            )
        try:
            if evaluate_branch(parent, env) != required:
                return False
        except (KeyError, TypeError, ValueError):
            return None
        current = parent
    return True


def _required_outputs(ir: FunctionIR, assignment: dict[str, Any] | None = None) -> list[str]:
    required = []
    if ir.ret_type not in ("", "void"):
        required.append("return")
    for param in ir.params:
        if param.is_ptr and param.is_written:
            if assignment is not None and (
                assignment.get(param.name) == 0
                or assignment.get(pointer_address_key(param.name)) == 0
            ):
                continue
            required.append(pointer_value_key(param.name))
    required.extend(memory.name for memory in ir.memory_vars if memory.write)
    required.extend(_global_output_columns(ir))
    return list(dict.fromkeys(required))


def _type_ok(value: Any, type_info: TypeInfo | None) -> bool:
    """Validate against extractor-owned TypeInfo without spelling inference."""
    if not isinstance(value, (int, float)):
        return True
    if type_info is None or type_info.kind == "unknown":
        return False
    if type_info.kind == "bool" and value not in {0, 1}:
        return False
    if type_info.enum_values and value not in type_info.enum_values.values():
        return False
    if type_info.min_value is not None and value < type_info.min_value:
        return False
    if type_info.max_value is not None and value > type_info.max_value:
        return False
    return True


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

    A ``for`` condition is still retained as a branch header for the target, but
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
    """Move derived branch values onto their controllable source.

    Automatic locals such as ``table[index].field`` or a local copied from a
    global are not target columns.  When the extractor supplied the typed
    source relation, move the finite candidate domain to the external driver
    instead of enumerating values that disappear during target projection.
    """
    by_name = {cv.name: cv for cv in ir.control_vars}
    by_var = {_norm(cv.var): cv for cv in ir.control_vars}
    for control in ir.control_vars:
        origin = _origin_record(control.value_origin)
        if origin is None:
            continue
        if origin.get("kind") == "local_from_global":
            driver_name = str(origin.get("driver", "")).strip()
            source = candidates.get(control.name) or candidates.get(control.var)
            if not driver_name or not source:
                candidates.pop(control.name, None)
                candidates.pop(control.var, None)
                continue
            target = candidates.setdefault(
                driver_name,
                {"cv": control, "values": set(), "enum": {}},
            )
            target["values"].update(source.get("values", set()))
            candidates.pop(control.name, None)
            candidates.pop(control.var, None)
            continue
        if origin.get("kind") != "const_table_field":
            continue
        table_values = origin.get("table_values", {})
        driver_name = str(origin.get("driver", ""))
        driver = by_name.get(driver_name) or by_var.get(_norm(driver_name))
        source = candidates.get(control.name) or candidates.get(control.var)
        if driver is None or not source or not isinstance(table_values, dict):
            candidates.pop(control.name, None)
            continue
        related_atoms = [
            atom for branch in ir.branches for atom in branch.atoms
            if _norm(atom.var) in {_norm(control.name), _norm(control.var)}
        ]
        if related_atoms:
            # The derived value is not itself a testcase column.  For a
            # branch predicate over that value, every extractor-proven table
            # index is a finite driver candidate; the targeted solver will
            # select the first index that proves the requested outcome.
            indexes = set()
            for raw_index in table_values:
                try:
                    indexes.add(int(raw_index))
                except (TypeError, ValueError):
                    continue
            target = candidates.setdefault(
                driver.name,
                {"cv": driver, "values": set(), "enum": {}},
            )
            target["values"].update(indexes)
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


def _repeated_or_variants(ir: FunctionIR, baseline: Any,
                          obligation: Any,
                          assignment: dict[str, Any]) -> tuple[dict[str, Any], ...] | None:
    """Return MC/DC witnesses for a repeated-variable equality OR chain.

    Repeated-variable conditions are one input dimension.  The independent
    true witnesses are the distinct equality literals; the false side uses
    the first and last extractor-proven values outside those literals.  This
    is the typed form of the baseline's common-variable situation and avoids
    a function-specific scenario table.
    """
    if getattr(obligation, "kind", None) != "mcdc":
        return None
    branch = next(
        (item for item in ir.branches
         if item.bid == getattr(obligation, "branch_id", None)), None,
    )
    if (branch is None or branch.connective != "||"
            or len(branch.atoms) < 2):
        return None
    atoms = branch.atoms
    controls = []
    for atom in atoms:
        control = next(
            (item for item in ir.control_vars
             if _norm(item.name) == _norm(atom.var)
             or _norm(item.var) == _norm(atom.var)), None,
        )
        if control is None:
            return None
        controls.append(control)
    if not controls or any(_norm(item.name) != _norm(controls[0].name)
                           for item in controls):
        return None
    if any(atom.op != "==" or atom.boundary is None for atom in atoms):
        return None
    try:
        index = int(obligation.condition_index)
    except (TypeError, ValueError):
        return None
    if index < 0 or index >= len(atoms):
        return None

    candidates = control_candidates(
        ir, boundary_policy=getattr(baseline, "boundary_policy", None),
    )
    entry = candidates.get(controls[0].name, {})
    values = sorted(entry.get("values", ()))
    literals = {atom.boundary for atom in atoms}
    if obligation.outcome:
        target_values = [atoms[index].boundary]
    else:
        outside = [value for value in values if value not in literals]
        if not outside:
            return None
        target_values = [outside[0], outside[-1]]

    variants: list[dict[str, Any]] = []
    for value in target_values:
        trial = dict(assignment)
        trial[controls[0].name] = value
        _clear_derived_bindings(trial, ir, {controls[0].name})
        env = _control_env(trial, ir)
        try:
            atom_values = [evaluate_atom(atom, env) for atom in atoms]
            expected_others = False
            independent = (
                atom_values[index] == obligation.outcome
                and all(value is expected_others
                        for pos, value in enumerate(atom_values)
                        if pos != index)
                and evaluate_branch(branch, env) == bool(obligation.outcome)
                and branch_path_reachable(ir, branch, env) is True
            )
        except (KeyError, TypeError, ValueError):
            independent = False
        if independent:
            trial.update(env)
            variants.append(trial)
    return tuple(variants) or (dict(assignment),)


def _logical_status_variants(ir: FunctionIR, baseline: Any,
                             obligation: Any,
                             assignment: dict[str, Any]
                             ) -> tuple[dict[str, Any], ...] | None:
    """Expand the typed status/global-field situation in a two-atom AND.

    Some embedded predicates combine a stub status with a request/category
    field selected through an array index.  The baseline keeps one MC/DC
    truth-vector witness, while the target contract also retains the
    extractor-proven categorical values that preserve that vector.  Vary one
    semantic input dimension at a time; never build a Cartesian product or
    infer values from a function name.
    """
    if (getattr(obligation, "kind", None) != "mcdc"
            or getattr(obligation, "condition_index", None) is None):
        return None
    branch = next(
        (item for item in ir.branches
         if item.bid == getattr(obligation, "branch_id", None)), None,
    )
    if (branch is None or branch.connective != "&&"
            or len(branch.atoms) != 2):
        return None
    controls = []
    for atom in branch.atoms:
        controls.append(next(
            (item for item in ir.control_vars
             if _norm(item.name) == _norm(atom.var)
             or _norm(item.var) == _norm(atom.var)), None,
        ))
    stub_indexes = [
        index for index, control in enumerate(controls)
        if control is not None and control.source == "stub"
    ]
    if len(stub_indexes) != 1:
        return None
    stub_index = stub_indexes[0]
    field_index = 1 - stub_index
    field = branch.atoms[field_index]
    field_key = next(
        (key for key in assignment if _norm(key) == _norm(field.var)), None,
    )
    if field_key is None or field.boundary is None or field.type_info is None:
        return None
    stub = branch.atoms[stub_index]
    if stub.boundary is None:
        return None
    candidates = control_candidates(
        ir, boundary_policy=getattr(baseline, "boundary_policy", None),
    )
    stub_control = controls[stub_index]
    stub_values = tuple(sorted(
        candidates.get(stub_control.name, {}).get("values", ())
    ))
    field_values = typed_status_points(
        field.boundary, field.type_info,
        getattr(baseline, "boundary_policy", None),
    )
    if not stub_values or not field_values:
        return None

    variants: list[dict[str, Any]] = [dict(assignment)]
    try:
        selected_index = int(obligation.condition_index)
    except (TypeError, ValueError):
        return None
    if selected_index != stub_index:
        return tuple(variants)

    if bool(obligation.outcome):
        # Keep the stub TRUE and retain additional categorical values for the
        # global field that still makes its comparison atom TRUE.
        for value in field_values:
            if value == assignment.get(field_key):
                continue
            trial = dict(assignment)
            trial[field_key] = value
            try:
                env = _control_env(trial, ir)
                atom_values = [evaluate_atom(atom, env) for atom in branch.atoms]
                valid = (
                    atom_values[selected_index] is bool(obligation.outcome)
                    and all(value is True for index, value in enumerate(atom_values)
                            if index != selected_index)
                    and evaluate_branch(branch, env) is True
                    and branch_path_reachable(ir, branch, env) is True
                )
            except (KeyError, TypeError, ValueError):
                valid = False
            if valid:
                trial.update(env)
                variants.append(trial)
    else:
        # For the FALSE stub witness, retain the non-boundary status codes
        # that keep the other atom TRUE.
        current = assignment.get(stub_control.name)
        for value in stub_values:
            if value == current:
                continue
            trial = dict(assignment)
            trial[stub_control.name] = value
            _clear_derived_bindings(trial, ir, {stub_control.name})
            try:
                env = _control_env(trial, ir)
                atom_values = [evaluate_atom(atom, env) for atom in branch.atoms]
                valid = (
                    atom_values[selected_index] is bool(obligation.outcome)
                    and all(value is True for index, value in enumerate(atom_values)
                            if index != selected_index)
                    and evaluate_branch(branch, env) is False
                    and branch_path_reachable(ir, branch, env) is True
                )
            except (KeyError, TypeError, ValueError):
                valid = False
            if valid:
                trial.update(env)
                variants.append(trial)
    return tuple(variants)


def _clear_derived_bindings(values: dict[str, Any], ir: FunctionIR,
                            drivers: set[str]) -> None:
    """Remove stale automatic locals before replaying a driver value.

    A solver result contains the derived value that made its witness
    executable.  When a coverage family varies the underlying table index,
    retaining that local would make the new driver and the old derived value
    disagree.  The next ``_control_env`` call then resolves the value again
    from the extractor-owned origin relation.
    """
    normalized = {_norm(item) for item in drivers}
    for control in ir.control_vars:
        origin = _origin_record(control.value_origin)
        if not isinstance(origin, dict):
            continue
        if origin.get("kind") not in {"const_table_field", "local_from_global"}:
            continue
        driver = _norm(str(origin.get("driver", "")))
        if driver not in normalized:
            continue
        values.pop(control.name, None)
        values.pop(control.var, None)


def _index_driver_limit(ir: FunctionIR, driver_name: str) -> int | None:
    """Return the proven common bound for one dynamic index driver.

    A table may contain a sentinel entry that is not present in a companion
    buffer.  The executable domain is therefore the intersection of the
    extractor-recorded array bounds for objects indexed by the same driver,
    not the size of whichever table happened to produce a derived value.
    """
    return _semantic_index_driver_limit(ir, driver_name)


def _ancestor_branches(ir: FunctionIR, branch) -> tuple:
    """Return lexical branch parents from the extractor's parent_bid facts."""
    by_id = {item.bid: item for item in ir.branches}
    result = []
    parent_id = getattr(branch, "parent_bid", None)
    while parent_id in by_id:
        parent = by_id[parent_id]
        result.append(parent)
        parent_id = getattr(parent, "parent_bid", None)
    return tuple(result)


def _ancestor_uses_driver(ir: FunctionIR, branch, driver_name: str) -> bool:
    """Detect an index already participating in an enclosing branch.

    A nested branch over the same array index is a local viewpoint of the
    enclosing scenario.  Expanding it to every array element would duplicate
    the parent scenario rather than add a new semantic obligation.  This is
    determined from typed control origins and expression variables, never
    from a function name or a Golden row.
    """
    wanted = _norm(driver_name)
    controls = {control.name: control for control in ir.control_vars}
    controls.update({_norm(control.var): control for control in ir.control_vars})
    for parent in _ancestor_branches(ir, branch):
        for atom in parent.atoms:
            if wanted == _norm(atom.var) or wanted in _norm(atom.var):
                return True
            control = controls.get(atom.var) or controls.get(_norm(atom.var))
            origin = _origin_record(control.value_origin) if control else None
            if isinstance(origin, dict) and wanted == _norm(
                    str(origin.get("driver", ""))):
                return True
    return False


def _clip_index_candidates(ir: FunctionIR, candidates: dict) -> None:
    """Clip generated index witnesses to the extractor-proven common domain.

    ``_remap_derived_candidates`` can move a finite table relation onto its
    index driver, but the relation alone may include a sentinel entry that is
    absent from a companion buffer.  Keep the clipping at the generic input
    boundary so branch, condition, and boundary obligations all share the
    same executable array domain.
    """
    for name, entry in candidates.items():
        limit = _index_driver_limit(ir, name)
        if limit is None:
            continue
        values = {
            value for value in entry.get("values", set())
            if isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value < limit
        }
        if values:
            values.add(limit - 1)
        entry["values"] = values


def _array_index_coverage(baseline: Any) -> tuple[bool, bool]:
    """Return fixed-array and table-array coverage modes from the contract.

    ``psd-rebuild@1.0`` predates the explicit array comparison classes, so its
    legacy fields retain their established behavior.  New baselines must use
    ``comparison_classes``; the implementation does not infer table coverage
    from a function, project, or Golden artifact.
    """
    policy = getattr(baseline, "array_policy", {})
    if not isinstance(policy, dict):
        return False, False
    classes = policy.get("comparison_classes")
    if isinstance(classes, dict):
        indexed = classes.get("indexed_array", {})
        table = classes.get("table_array", {})
        fixed_index = (isinstance(indexed, dict)
                       and indexed.get("selected_index") == "fixed")
        table_all_indexes = (isinstance(table, dict)
                             and table.get("index_coverage") == "all")
        return fixed_index, table_all_indexes
    legacy_fixed_index = bool(policy.get("fixed_index", False))
    return legacy_fixed_index, legacy_fixed_index


def _coverage_variants(ir: FunctionIR, baseline: Any,
                       obligation: Any,
                       assignment: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Expand one proof witness for an extractor-proven table-index case.

    The array policy describes a semantic situation, not a function-specific
    exception: when a branch compares a value produced by a finite constant
    table, every table index is an executable viewpoint.  The C++ extractor
    supplies the table relation and its driver; this helper only replays that
    typed relation and never reads a Golden or parses source text.
    """
    repeated_or = _repeated_or_variants(ir, baseline, obligation, assignment)
    if repeated_or is not None:
        return repeated_or
    logical_status = _logical_status_variants(
        ir, baseline, obligation, assignment,
    )
    if logical_status is not None:
        return logical_status
    fixed_index, table_all_indexes = _array_index_coverage(baseline)
    if (getattr(obligation, "kind", None) != "branch"
            or getattr(obligation, "outcome", None) is None
            or not (fixed_index or table_all_indexes)):
        return (dict(assignment),)
    branch = next(
        (item for item in ir.branches
         if item.bid == getattr(obligation, "branch_id", None)), None,
    )
    if branch is None:
        return (dict(assignment),)
    by_name = {str(control.name): control for control in ir.control_vars}
    by_var = {_norm(control.var): control for control in ir.control_vars}
    drivers: dict[str, list[int]] = {}
    if table_all_indexes:
        for atom in branch.atoms:
            control = by_name.get(str(atom.var)) or by_var.get(_norm(atom.var))
            origin = _origin_record(control.value_origin) if control else None
            if (not isinstance(origin, dict)
                    or origin.get("kind") != "const_table_field"):
                continue
            driver_name = str(origin.get("driver", ""))
            driver = by_name.get(driver_name) or by_var.get(_norm(driver_name))
            table_values = origin.get("table_values")
            if driver is None or not isinstance(table_values, dict):
                continue
            indexes: list[int] = []
            for raw_index in table_values:
                try:
                    indexes.append(int(raw_index))
                except (TypeError, ValueError):
                    continue
            if indexes:
                limit = _index_driver_limit(ir, driver.name)
                if limit is not None:
                    indexes = [index for index in indexes if 0 <= index < limit]
                if indexes:
                    drivers[driver.name] = sorted(set(indexes))

    # A direct scalar comparison over a dynamic array index is the same
    # fixed-index coverage family even when no derived table field participates
    # in the predicate.  Do not expand a driver that already has a derived
    # table relation: in that situation the branch's typed table witnesses are
    # the controlling domain and expanding the raw parameter would duplicate
    # or invent cases for the same semantic situation.
    derived_drivers = {
        _norm(str(_origin_record(control.value_origin).get("driver", "")))
        for control in ir.control_vars
        if isinstance(_origin_record(control.value_origin), dict)
        and _origin_record(control.value_origin).get("kind") == "const_table_field"
    }
    if fixed_index:
        for atom in branch.atoms:
            control = by_name.get(str(atom.var)) or by_var.get(_norm(atom.var))
            if control is None:
                continue
            if control.source not in {"param", "global", "local_from_global"}:
                continue
            limit = _index_driver_limit(ir, control.name)
            if limit is not None:
                if (_norm(control.name) in derived_drivers
                        and not _ancestor_uses_driver(ir, branch, control.name)):
                    continue
                if _ancestor_uses_driver(ir, branch, control.name):
                    # Keep the approved typed boundary representatives for a
                    # nested viewpoint.  The common index domain's endpoints are
                    # already present after candidate clipping.
                    indexes = list(typed_boundary_points(
                        getattr(atom, "boundary", None),
                        getattr(atom, "type_info", None),
                        getattr(baseline, "boundary_policy", None),
                    ))
                    indexes = [
                        index for index in indexes
                        if isinstance(index, int) and 0 <= index < limit
                    ]
                    indexes.extend((0, limit - 1))
                    # Preserve the solver's reachable witness even when the
                    # enclosing branch excludes the lowest table indexes.  The
                    # boundary representatives are an expansion of that witness,
                    # not a replacement for it.
                    current_index = assignment.get(control.name)
                    if (isinstance(current_index, int)
                            and not isinstance(current_index, bool)
                            and 0 <= current_index < limit):
                        indexes.append(current_index)
                    indexes = sorted(set(indexes))
                    if not indexes:
                        indexes = [0, limit - 1]
                    drivers.setdefault(control.name, indexes)
                else:
                    drivers.setdefault(control.name, list(range(limit)))
    if not drivers:
        return (dict(assignment),)

    # Multiple independent table drivers form a product.  Keep the same
    # deterministic solver safety bound used by the generation pipeline.
    variants: list[dict[str, Any]] = [dict(assignment)]
    for driver_name in sorted(drivers):
        expanded: list[dict[str, Any]] = []
        for current in variants:
            for index in drivers[driver_name]:
                trial = dict(current)
                trial[driver_name] = index
                _clear_derived_bindings(trial, ir, {driver_name})
                env = _control_env(trial, ir)
                try:
                    reachable = branch_path_reachable(ir, branch, env)
                    matches = evaluate_branch(branch, env) == obligation.outcome
                except (KeyError, TypeError, ValueError):
                    continue
                if reachable is True and matches:
                    expanded.append(trial)
                if len(expanded) >= 4096:
                    break
            if len(expanded) >= 4096:
                break
        variants = expanded
        if not variants:
            return (dict(assignment),)
    return tuple(variants)


def _pointer_initial_value(selected: dict[str, Any], param) -> Any | None:
    """Read the caller-owned pointee value when the AST exposed a read path."""
    name = str(param.name)
    for key in (
        pointer_value_key(name),
        pointer_value_key(name, f"{name}[0]"),
        pointer_value_key(name, f"*{name}"),
        name,
    ):
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
    return _effect_expression_value(ir, effect, env)


def _guards_active(ir: FunctionIR, guards: Any, env: dict[str, Any],
                   offset: Any = None) -> bool | None:
    """Evaluate an extractor guard list without treating unknown as false."""
    if offset is not None:
        switch_path = _switch_offset_reachable(ir, offset, env)
        if switch_path is not True:
            return switch_path
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
            path = branch_path_reachable(ir, branch, env)
            if path is not True:
                return path
            active = evaluate_branch(branch, env)
        except (KeyError, TypeError, ValueError):
            return None
        if active != bool(guard.get("then")):
            return False
    return True


def _stub_return_slot(ir: FunctionIR, callee: str, call_order: Any,
                      call_offset: Any = None) -> int:
    """Map a Clang call order/offset to the visible target return slot."""
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
    candidates = call_return_keys(callee, slot)
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
    field = str(origin.get("field", "")).strip().lstrip(".")
    candidates = (
        (call_param_key(callee, index, slot, field),
         *call_param_keys(callee, index, slot))
        if field else call_param_keys(callee, index, slot)
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
    The target adapter exposes the return object field-by-field.  Keep that conversion in
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
        candidates = call_return_keys(callee, slot, field)
        for candidate in candidates:
            try:
                values[field] = _lookup(env, candidate)
                break
            except KeyError:
                continue
    return values


def _local_value_effects(ir: FunctionIR) -> list[dict[str, Any]]:
    return _effect_records(ir.local_value_effects)


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
        active = _guards_active(
            ir, effect.get("guards", []), env,
            effect.get("source_offset"),
        )
        if active is not True:
            continue
        constant = effect.get("constant_value")
        expression = str(effect.get("value", "")).strip()
        if constant is not None:
            value = constant
        else:
            value = _effect_expression_value(
                ir, effect, env, set(seen),
            )
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
                       before_offset: int | None = None,
                       exclude_call_offset: int | None = None) -> Any | None:
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
        origin = _origin_record(effect.get("origin"))
        if exclude_call_offset is not None and isinstance(origin, dict) \
                and origin.get("kind") == "stub_return":
            try:
                origin_call_offset = int(origin.get("call_offset"))
            except (TypeError, ValueError):
                origin_call_offset = None
            if origin_call_offset == exclude_call_offset:
                continue
        if _guards_active(
                ir, effect.get("guards", []), env,
                effect.get("source_offset")) is not True:
            continue
        constant = effect.get("constant_value")
        if constant is not None:
            return constant
        expression = str(effect.get("value", "")).strip()
        value = _effect_expression_value(ir, effect, env)
        if value is not None:
            return value
    return None


def _call_extension(call: Any, name: str) -> Any:
    extensions = getattr(call, "extensions", {})
    if not isinstance(extensions, dict) or name not in extensions:
        return None
    return extensions[name]


def _call_slot_environments(ir: FunctionIR, call: Any,
                            selected: dict[str, Any]
                            ) -> tuple[dict[str, Any], ...] | None:
    base = _control_env(selected, ir)
    raw_loops = _call_extension(call, "execution_loops")
    try:
        capacity = max(1, int(call.max_occurrences))
    except (AttributeError, TypeError, ValueError):
        capacity = 1
    if not raw_loops:
        return (base,) if capacity == 1 else None
    if not isinstance(raw_loops, list):
        return None
    loop_values: list[list[tuple[str, int]]] = []
    for raw_loop in raw_loops:
        if not isinstance(raw_loop, dict):
            return None
        driver = raw_loop.get("driver")
        if not isinstance(driver, str) or not driver.strip():
            return None
        try:
            start = int(raw_loop["start"])
            step = int(raw_loop["step"])
            count = int(raw_loop["count"])
        except (KeyError, TypeError, ValueError):
            return None
        if count <= 0 or step == 0:
            return None
        loop_values.append([
            (driver, start + step * index) for index in range(count)
        ])
    environments: list[dict[str, Any]] = []
    for combination in product(*loop_values):
        environment = dict(base)
        for driver, value in combination:
            environment[_norm(driver)] = value
            environment[driver] = value
        reachable = _call_is_reachable(ir, call, environment)
        if reachable is None:
            return None
        if reachable:
            environments.append(environment)
    if len(environments) != capacity:
        return None
    return tuple(environments)


def _stub_pointer_output_values(ir: FunctionIR,
                                selected: dict[str, Any]
                                ) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for call in _stub_calls(ir):
        output_keys = set(_semantic_call_output_columns(ir, call))
        if not output_keys:
            continue
        origins = _call_extension(call, "caller_param_origins")
        if not isinstance(origins, dict):
            continue
        slot_environments = _call_slot_environments(ir, call, selected)
        if slot_environments is None:
            continue
        call_span = _source_span(call)
        if call_span is None:
            continue
        for index, param in enumerate(call.params):
            if not param.is_ptr:
                continue
            info = call.pointer_arguments.get(str(index), {}) \
                if isinstance(call.pointer_arguments, dict) else {}
            if not isinstance(info, dict) or info.get("pointee_write"):
                continue
            fields = _semantic_call_param_fields(call, index)
            origin = origins.get(str(index), origins.get(index))
            if not isinstance(origin, dict) \
                    or origin.get("kind") != "local":
                continue
            root = str(origin.get("driver", "")).strip()
            if not root or not fields:
                continue
            keys = [
                call_param_key(str(call.callee), index, slot, field)
                for field in fields
                for slot in range(len(slot_environments))
            ]
            if not any(key in output_keys for key in keys):
                continue
            for slot, environment in enumerate(slot_environments):
                for field in fields:
                    key = call_param_key(
                        str(call.callee), index, slot, field,
                    )
                    if key not in output_keys:
                        continue
                    value = _local_field_value(
                        ir, root, field, environment,
                        before_offset=call_span[0],
                        exclude_call_offset=call_span[0],
                    )
                    if value is None:
                        break
                    values[key] = value
    return values


def _return_value(ir: FunctionIR, selected: dict[str, Any]) -> Any | None:
    """Prove the tested-function return for the selected AST path."""
    raw = _effect_records(ir.return_effects)
    if not raw:
        return None
    env = _control_env(selected, ir)
    applicable: list[Any] = []
    for effect in raw:
        if _guards_active(
                ir, effect.get("guards", []), env,
                effect.get("source_offset")) is not True:
            continue
        constant = effect.get("constant_value")
        if constant is not None:
            value = constant
        else:
            value = _effect_expression_value(ir, effect, env)
            if value is None:
                return None
        applicable.append(value)
    if not applicable:
        return None
    return applicable[-1] if all(item == applicable[-1] for item in applicable) else None


def _pointer_output_columns(param, effects: list[dict[str, Any]]) -> list[str]:
    name = str(param.name)
    columns: list[str] = []
    for effect in effects:
        path = str(effect.get("path", "")).strip()
        if not path or path == name:
            continue
        column = pointer_value_key(name, path)
        if column not in columns:
            columns.append(column)
    return columns


def _pointer_output_values(ir: FunctionIR, param,
                           selected: dict[str, Any]) -> dict[str, Any] | None:
    """Prove each caller-visible pointer output path from AST effects."""
    effects = _effect_records(param.write_effects)
    if not effects:
        return None
    env = _control_env(selected, ir)
    output_effects = [effect for effect in effects if isinstance(effect, dict)
                      and effect.get("path")]
    columns = _pointer_output_columns(param, output_effects)
    if not columns:
        return None
    values: dict[str, Any] = {}
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
            column = pointer_value_key(name, path)
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
            expected["return"] = value
    global_values = _global_output_values(ir, selected)
    if global_values is not None:
        expected.update(global_values)
    # Call-count comparison fields are semantic observations.  The target
    # adapter owns their concrete comparison-column spelling.
    call_counts = _call_execution_counts(ir, selected)
    if call_counts is not None:
        for callee, value in call_counts.items():
            expected[call_count_key(callee)] = value
    expected.update(_stub_pointer_output_values(ir, selected))
    for param in ir.params:
        if not param.is_ptr or not param.is_written:
            continue
        if selected.get(param.name) == 0 or selected.get(pointer_address_key(param.name)) == 0:
            continue
        pointer_values = _pointer_output_values(ir, param, selected)
        if pointer_values is not None:
            expected.update(pointer_values)
        value = _pointer_output_value(ir, param, selected)
        if value is not None:
            expected[pointer_value_key(param.name)] = value

    # Stub argument write-back columns are observable target outputs for the
    # ordinary non-Rte helpers.  Their deterministic generic oracle is the
    # selected input slot; callee side effects remain represented separately
    # by AST-proven tested-function pointer/global write effects.
    _, stub_output_columns = _semantic_call_columns(ir)
    for column in stub_output_columns:
        if column in expected:
            continue
        try:
            expected[column] = _lookup(selected, column)
        except KeyError:
            # The corresponding exact input key is installed by
            # ``_generic_inputs``.  Keeping this guard explicit makes an
            # incomplete legacy IR remain NEEDS_REVIEW instead of silently
            # inventing an output value.
            continue
    return expected


def validate_intent(ir: FunctionIR, intent: TestIntent, *,
                    evaluation: Any | None = None) -> ValidationResult:
    """Validate the legacy intent contract and optional semantic result.

    Formal Baseline generation passes its ``EvaluationResult`` so branch
    validation consumes the evaluator's observed decision.  The historical
    path keeps its previous behavior when no evaluation is supplied.
    """
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
        if _is_memory_helper(call) or call.ptr_call:
            continue
        if not any(call_count_key(call.callee) in str(key)
                   for key in intent.inputs):
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
        if not _type_ok(value, cv.type_info):
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
                    if not _switch_obligation_matches(
                            branch, obligation, actual):
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
                if evaluation is not None:
                    observed = evaluation.observed
                    actual = (
                        observed.get("decision")
                        if isinstance(observed, dict) and "decision" in observed
                        else observed
                    )
                else:
                    # Kept only for the historical intent API.  Formal
                    # generation never uses expected values to prove a
                    # source decision.
                    actual = evaluate_branch(
                        branch, env, _expanded_env(intent.expected)
                    )
                if actual != obligation.outcome:
                    errors.append(
                        f"分支结果不符: {branch.bid} 期望={obligation.outcome} 实际={actual}"
                    )
                else:
                    checks.append("branch-outcome")
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"分支不可证明: {branch.bid}: {exc}")

    for name in _required_outputs(ir, intent.inputs):
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


def _source_span(value: Any) -> tuple[int, int] | None:
    provenance = getattr(value, "provenance", None)
    expansion = getattr(provenance, "expansion", None)
    if expansion is None:
        return None
    try:
        start = int(expansion.offset)
        end = int(expansion.end_offset)
    except (TypeError, ValueError):
        return None
    if start < 0 or end < start:
        return None
    return start, end


def _switch_cases_for_branch(switch: Branch, branch: Branch) -> tuple[Any, ...]:
    """Find the switch cases whose extractor spans contain a child branch."""
    branch_span = _source_span(branch)
    if branch_span is not None:
        contained = []
        for case in switch.cases:
            case_span = _source_span(case)
            if (case_span is not None
                    and case_span[0] <= branch_span[0]
                    and branch_span[1] <= case_span[1]):
                contained.append(case)
        if contained:
            return tuple(contained)

    branch_line = int(getattr(branch, "line", 0) or 0)
    case_lines = [
        int(getattr(case.provenance.expansion, "line", 0) or 0)
        if getattr(case, "provenance", None) is not None
        and getattr(case.provenance, "expansion", None) is not None
        else 0
        for case in switch.cases
    ]
    selected = []
    for index, case in enumerate(switch.cases):
        case_line = case_lines[index]
        next_lines = [line for line in case_lines[index + 1:] if line]
        next_line = min(next_lines, default=None)
        if case_line and branch_line >= case_line \
                and (next_line is None or branch_line < next_line):
            selected.append(case)
    return tuple(selected)


def _switch_selector_index_limit(ir: FunctionIR,
                                 selector: str) -> int | None:
    wanted = _norm(selector)
    limits = [
        _index_driver_limit(ir, driver)
        for raw in ir.global_objects
        for driver in getattr(raw, "index_drivers", [])
        if _norm(driver) == wanted
    ]
    values = [limit for limit in limits if limit is not None]
    return min(values) if values else None


def _switch_obligation_matches(branch: Branch,
                               obligation: TestObligation,
                               selector: Any) -> bool:
    case = _find_switch_case(branch, obligation)
    if case is None or not _switch_case_matches(case, selector, branch.cases):
        return False
    boundary = obligation.boundary_value
    return boundary is None or selector == boundary


def _table_guard_branch(ir: FunctionIR, call: Any,
                        guard: dict[str, Any]) -> Branch | None:
    global_name = guard.get("global")
    index_var = guard.get("index_var")
    field = guard.get("field")
    operator = guard.get("op")
    boundary = guard.get("boundary")
    if not all(isinstance(item, str) and item.strip()
               for item in (global_name, index_var, field, operator)):
        return None
    if not isinstance(guard.get("then"), bool) or boundary is None:
        return None
    target = _norm(f"{global_name}[{index_var}].{field}")
    candidates: list[Branch] = []
    for branch in ir.branches:
        for atom in branch.atoms:
            extensions = atom.extensions if isinstance(atom.extensions, dict) else {}
            canonical_var = (
                extensions["canonical_var"]
                if "canonical_var" in extensions else None
            )
            paths = (atom.var, canonical_var)
            if (target not in {_norm(path) for path in paths if path}
                    or atom.op != operator or atom.boundary != boundary):
                continue
            candidates.append(branch)
            break
    if not candidates:
        return None
    call_span = _source_span(call)
    if call_span is None:
        return candidates[0] if len(candidates) == 1 else None
    contained = [
        branch for branch in candidates
        if (branch_span := _source_span(branch)) is not None
        and branch_span[0] <= call_span[0]
        and call_span[1] <= branch_span[1]
    ]
    if len(contained) == 1:
        return contained[0]
    return None


def _call_is_reachable(ir: FunctionIR, call: Any,
                       env: dict[str, Any]) -> bool | None:
    for guard in getattr(call, "guards", ()):
        if not isinstance(guard, dict):
            return None
        branch = next((item for item in ir.branches
                       if item.bid == guard.get("bid")), None) \
            if guard.get("bid") is not None else _table_guard_branch(
                ir, call, guard,
            )
        if branch is None:
            return None
        path = branch_path_reachable(ir, branch, env)
        if path is not True:
            return path
        try:
            if evaluate_branch(branch, env) != bool(guard.get("then")):
                return False
        except (KeyError, TypeError, ValueError):
            return None

    call_span = _source_span(call)
    if call_span is None:
        return True if not getattr(call, "guards", ()) else None
    for switch in (item for item in ir.branches if item.kind == "switch"):
        switch_span = _source_span(switch)
        if (switch_span is None
                or switch_span[0] > call_span[0]
                or call_span[1] > switch_span[1]):
            continue
        cases = _switch_cases_for_branch(switch, call)
        if not cases:
            return None
        try:
            selector = _switch_selector_value(switch, ir, env)
        except (KeyError, TypeError, ValueError):
            return None
        if not any(
                _switch_case_matches(case, selector, switch.cases)
                for case in cases):
            return False
    return True


def _switch_offset_reachable(ir: FunctionIR, offset: Any,
                             env: dict[str, Any]) -> bool | None:
    try:
        target = int(offset)
    except (TypeError, ValueError):
        return True
    for switch in (item for item in ir.branches if item.kind == "switch"):
        switch_span = _source_span(switch)
        if (switch_span is None
                or target < switch_span[0]
                or target > switch_span[1]):
            continue
        cases = [
            case for case in switch.cases
            if (case_span := _source_span(case)) is not None
            and case_span[0] <= target <= case_span[1]
        ]
        if not cases:
            return None
        try:
            selector = _switch_selector_value(switch, ir, env)
        except (KeyError, TypeError, ValueError):
            return None
        if not any(
                _switch_case_matches(case, selector, switch.cases)
                for case in cases):
            return False
    return True


def _call_execution_counts(ir: FunctionIR,
                           selected: dict[str, Any]) -> dict[str, int] | None:
    env = _control_env(selected, ir)
    counts: dict[str, int] = {}
    unknown: set[str] = set()
    for call in ir.calls:
        if _is_memory_helper(call) or call.ptr_call:
            continue
        callee = str(call.callee or "").strip()
        if not callee:
            continue
        reachable = _call_is_reachable(ir, call, env)
        if reachable is None:
            unknown.add(callee)
        elif reachable:
            try:
                occurrences = max(1, int(call.max_occurrences))
            except (AttributeError, TypeError, ValueError):
                occurrences = 1
            counts[callee] = counts.get(callee, 0) + occurrences
        else:
            counts.setdefault(callee, 0)
    if unknown:
        return None
    return counts


def _apply_switch_path_target(ir: FunctionIR,
                              domains: dict[str, list[Any]],
                              raw: dict[str, Any],
                              branch: Branch) -> bool:
    """Choose a proven selector value for every enclosing switch case."""
    by_id = {item.bid: item for item in ir.branches}
    current = branch
    visited: set[str] = set()
    while current.parent_bid:
        if current.bid in visited:
            return False
        visited.add(current.bid)
        parent = by_id.get(current.parent_bid)
        if parent is None:
            return False
        if parent.kind == "switch":
            cases = _switch_cases_for_branch(parent, current)
            selector = parent.selector
            selector_expression = (
                selector.driver or selector.expression
                if selector is not None else ""
            )
            key = _domain_key_for(ir, selector_expression, domains)
            if not cases or key is None:
                return False
            selected = None
            for value in domains.get(key, ()):
                if any(_switch_case_matches(case, value, parent.cases)
                       for case in cases):
                    selected = value
                    break
            if selected is None:
                return False
            raw[key] = selected
        current = parent
    return True


def _find_switch_case(branch: Branch, obligation: TestObligation):
    wanted = (obligation.case_label or obligation.description or "").strip()
    for case in branch.cases:
        if wanted in {case.label, _case_obligation_label(case)}:
            return case
        if case.is_default and wanted.lower().startswith("default"):
            return case
    return None


def _switch_selector_value(branch: Branch, ir: FunctionIR,
                           env: dict[str, Any]) -> Any:
    """Resolve a switch selector from the proof environment only.

    The selector may be an automatic loop/local variable.  It is still a
    useful proof value, but it must not become a rendered target input column.
    """
    selector = branch.selector
    if selector is None:
        raise KeyError(f"switch {branch.bid} 缺少 selector fact")
    condition = selector.driver or selector.expression
    try:
        return _lookup(env, condition)
    except KeyError:
        for control in ir.control_vars:
            if condition not in {control.var, control.name}:
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
            if (_is_memory_helper(call) or call.ptr_call):
                continue
            scenario_inputs.setdefault(call_count_key(call.callee), 0)
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


def _generic_inputs(ir: FunctionIR,
                    baseline: Any | None = None
                    ) -> tuple[dict[str, list[Any]], dict[str, Any]]:
    boundary_policy = getattr(baseline, "boundary_policy", None)
    candidates = control_candidates(ir, boundary_policy=boundary_policy)
    _remap_derived_candidates(ir, candidates)
    _clip_index_candidates(ir, candidates)
    loop_locals = _loop_only_local_controls(ir)
    derivable_locals = {
        str(item.get("name"))
        for item in _local_value_effects(ir)
        if item.get("name") and not item.get("path")
    }
    allowed = {
        key
        for cv in ir.control_vars
        for key in (cv.name, cv.var)
        if key and cv.constant_value is None
        and cv.source in ("param", "global", "local_from_global", "stub")
    }
    # A local_from_global control is evaluated through its extractor-proven
    # external driver.  The driver may be a GlobalObject rather than a
    # ControlVar, so add it explicitly to the solver domain allow-list.
    for control in ir.control_vars:
        origin = _origin_record(control.value_origin)
        if (control.source == "local_from_global"
                and isinstance(origin, dict)
                and origin.get("kind") == "local_from_global"):
            driver = str(origin.get("driver", "")).strip()
            if driver:
                allowed.add(driver)
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
        # from the target input columns.
        if (name in allowed or name in loop_locals) and item["values"]
    }
    # A local receive buffer is not a testcase column by its source name.  The
    # extractor records its value origin as a typed Rte_Read stub parameter;
    # expose that same call slot as the finite solver dimension so the local
    # condition can be varied without promoting the automatic variable itself.
    for control in ir.control_vars:
        origin = _origin_record(control.value_origin)
        if control.source != "local" or not isinstance(origin, dict):
            continue
        if origin.get("kind") != "stub_param":
            continue
        callee = str(origin.get("callee", ""))
        if not callee:
            continue
        try:
            index = int(origin.get("index"))
        except (TypeError, ValueError):
            continue
        entry = candidates.get(control.var) or candidates.get(control.name)
        if not entry or not entry.get("values"):
            continue
        slot = _stub_return_slot(
            ir, callee, origin.get("call_order"), origin.get("call_offset"),
        )
        field = str(origin.get("field", "")).strip().lstrip(".")
        domains[call_param_key(callee, index, slot, field or None)] = sorted(
            entry["values"],
        )
    fixed: dict[str, Any] = {}
    for cv in ir.control_vars:
        if cv.constant_value is not None:
            fixed[cv.name] = cv.constant_value
    for param in ir.params:
        if not param.is_ptr:
            # A scalar formal is controllable even when it only drives an
            # array/table access and never appears in a branch predicate.
            # Materialize it here so validation and CSV rendering cannot
            # mistake an omitted parameter for an unresolved local.
            fixed.setdefault(param.name, 0)
            continue
        # The valid-pointer proof value is semantic.  The WinAMS adapter
        # converts the corresponding address key to its target address.
        fixed[param.name] = 1
        fixed[pointer_address_key(param.name)] = 1
        # Address columns and dereferenced value columns are distinct
        # target variables.  A generic row starts with a deterministic zero
        # pointee; AST write effects may replace it in the expected half.
        fixed[pointer_value_key(param.name)] = 0
        fixed[pointer_value_key(param.name, f"{param.name}[0]")] = 0
        fixed[pointer_value_key(param.name, f"*{param.name}")] = 0
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
    # Call-count state is an execution pre-state.  Slot capacity belongs to
    # the stub declaration/column shape, not to each testcase's input value.
    for call in ir.calls:
        if _is_memory_helper(call) or call.ptr_call:
            continue
        fixed[call_count_key(call.callee)] = 0
    stub_input_columns, stub_return_columns = _semantic_call_columns(ir)
    for column in (*stub_input_columns, *stub_return_columns):
        fixed[column] = 0
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
                selector = branch.selector
                selector_names = {
                    name for name in (
                        selector.driver if selector else None,
                        selector.expression if selector else None,
                    ) if name
                }
                control = next(
                    (item for item in ir.control_vars
                     if item.name in selector_names
                     or item.var in selector_names),
                    None,
                )
                default_points = (
                    switch_default_points(
                        branch.cases,
                        control.type_info if control is not None else None,
                    )
                    if control is not None else ()
                )
                selector_expression = (
                    selector.driver if selector else None
                ) or (selector.expression if selector else "")
                selector_limit = _switch_selector_index_limit(
                    ir, selector_expression,
                ) if selector_expression else None
                if selector_limit is not None:
                    default_points = tuple(
                        point for point in default_points
                        if isinstance(point, int)
                        and 0 <= point < selector_limit
                    )
                    if selector_limit > 0:
                        explicit_values = {
                            case.value for case in branch.cases
                            if not case.is_default and case.value is not None
                        }
                        if selector_limit - 1 not in explicit_values:
                            default_points = tuple(sorted(
                                {*default_points, selector_limit - 1}
                            ))
                default_point = default_points[0] if default_points else None
                for case_index, case in enumerate(branch.cases):
                    label = _case_obligation_label(case)
                    obligations.append(TestObligation(
                        oid=f"{branch.bid}:case:{case_index}",
                        kind="case", branch_id=branch.bid,
                        description=label, case_label=label,
                        boundary_value=(default_point
                                        if case.is_default else None),
                    ))
                default_label = next(
                    (_case_obligation_label(item) for item in branch.cases
                     if item.is_default),
                    "default:",
                )
                for point_index, point in enumerate(default_points[1:], 1):
                    obligations.append(TestObligation(
                        oid=f"{branch.bid}:default:{point_index}:{point}",
                        kind="case", branch_id=branch.bid,
                        description=f"组合(default:{point_index})",
                        case_label=default_label,
                        boundary_class="default",
                        boundary_value=point,
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
                        if _switch_obligation_matches(
                                branch, obligation, selector):
                            selected = env
                            break
                elif (
                    branch_path_reachable(ir, branch, env) is True
                    and evaluate_branch(branch, env) == obligation.outcome
                ):
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
        if branch and any(atom.mask is not None for atom in branch.atoms):
            trace_items.append(RuleTrace(
                "builtin.bitmask", "FunctionIR.Atom.text", "bitmask candidate proof"
            ))
        if any(param.is_ptr for param in ir.params):
            trace_items.append(RuleTrace(
                "builtin.pointer", "target address contract", "non-overlapping address"
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
            if control.var in domains:
                return control.var
            origin = _origin_record(control.value_origin)
            if isinstance(origin, dict) and origin.get("kind") == "const_table_field":
                driver = str(origin.get("driver", ""))
                for candidate in ir.control_vars:
                    if driver in {_norm(candidate.name), _norm(candidate.var)} \
                            and candidate.name in domains:
                        return candidate.name
            if isinstance(origin, dict) and origin.get("kind") == "local_from_global":
                driver = _norm(str(origin.get("driver", "")))
                for candidate in domains:
                    if _norm(candidate) == driver:
                        return candidate
            if isinstance(origin, dict) and origin.get("kind") == "stub_param":
                callee = str(origin.get("callee", ""))
                try:
                    index = int(origin.get("index"))
                except (TypeError, ValueError):
                    index = None
                if callee and index is not None:
                    slot = _stub_return_slot(
                        ir, callee, origin.get("call_order"),
                        origin.get("call_offset"),
                    )
                    field = str(origin.get("field", "")).strip().lstrip(".")
                    candidate = call_param_key(
                        callee, index, slot, field or None,
                    )
                    if candidate in domains:
                        return candidate
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


def _tree_output_assignment(tree: Any, desired: bool) -> dict[int, bool] | None:
    """Build one deterministic leaf assignment for a tree output."""
    if not isinstance(tree, dict):
        return None
    kind = tree.get("kind")
    if kind == "atom":
        try:
            return {int(tree["index"]): bool(desired)}
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "not":
        return _tree_output_assignment(tree.get("child"), not desired)
    if kind != "logical":
        return None
    children = tree.get("children")
    if not isinstance(children, list) or not children:
        return None
    op = tree.get("op")
    if op == "&&":
        child_values = ([True] * len(children) if desired else
                        [False] + [True] * (len(children) - 1))
    elif op == "||":
        child_values = ([True] + [False] * (len(children) - 1) if desired else
                        [False] * len(children))
    else:
        return None
    result: dict[int, bool] = {}
    for child, child_value in zip(children, child_values):
        assignment = _tree_output_assignment(child, child_value)
        if assignment is None:
            return None
        result.update(assignment)
    return result


def _condition_tree_mcdc_pair(tree: Any,
                              condition_index: int
                              ) -> tuple[dict[int, bool], dict[int, bool]] | None:
    """Return false/true leaf vectors that make one leaf independent.

    The extractor stores the actual logical tree, so MC/DC for a mixed
    expression must preserve the connective at each ancestor.  A selected
    leaf is independent when every sibling subtree is fixed to that
    ancestor's identity value (true for ``&&``, false for ``||``).  This
    yields one deterministic pair without enumerating the global input
    product.
    """
    if not isinstance(tree, dict):
        return None
    kind = tree.get("kind")
    if kind == "atom":
        try:
            if int(tree["index"]) != condition_index:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return ({condition_index: False}, {condition_index: True})
    if kind == "not":
        return _condition_tree_mcdc_pair(tree.get("child"), condition_index)
    if kind != "logical":
        return None
    children = tree.get("children")
    if not isinstance(children, list) or not children:
        return None
    op = tree.get("op")
    if op not in {"&&", "||"}:
        return None
    identity = op == "&&"
    for selected_index, child in enumerate(children):
        pair = _condition_tree_mcdc_pair(child, condition_index)
        if pair is None:
            continue
        false_vector, true_vector = pair
        for sibling_index, sibling in enumerate(children):
            if sibling_index == selected_index:
                continue
            assignment = _tree_output_assignment(sibling, identity)
            if assignment is None:
                return None
            false_vector.update(assignment)
            true_vector.update(assignment)
        return false_vector, true_vector
    return None


def _mcdc_expected_truths(branch: Branch, condition_index: int,
                          outcome: bool) -> dict[int, bool] | None:
    """Return the leaf truth vector for one side of an MC/DC pair."""
    if branch.condition_tree is not None:
        pair = _condition_tree_mcdc_pair(branch.condition_tree, condition_index)
        if pair is None:
            return None
        return pair[1 if outcome else 0]
    if (branch.connective or "") not in {"&&", "||"}:
        return None
    other = branch.connective == "&&"
    return {
        index: bool(outcome) if index == condition_index else other
        for index in range(len(branch.atoms))
    }


def _branch_target_atoms(branch: Branch, outcome: bool) -> list[tuple[int, bool]]:
    """Return sufficient atom truth targets for one branch outcome."""
    atoms = list(branch.atoms)
    if branch.condition_tree is not None:
        return _condition_target_atoms(branch.condition_tree, outcome)
    connective = branch.connective or "single"
    if connective == "&&":
        desired = [True] * len(atoms) if outcome else [False]
    elif connective == "||":
        desired = [False] * len(atoms) if not outcome else [True]
    else:
        desired = [outcome]
    if connective == "&&" and not outcome:
        return [(0, False)] if atoms else []
    if connective == "||" and outcome:
        return [(0, True)] if atoms else []
    return list(enumerate(desired))


def _ancestor_requirements(ir: FunctionIR, branch: Branch) -> list[tuple[Branch, bool]]:
    """Return extractor-proven parent branch outcomes, outermost first."""
    by_id = {item.bid: item for item in ir.branches}
    result: list[tuple[Branch, bool]] = []
    current = branch
    visited: set[str] = set()
    while current.parent_bid:
        if current.bid in visited:
            return []
        visited.add(current.bid)
        parent = by_id.get(current.parent_bid)
        if parent is None:
            return []
        if parent.kind != "switch":
            if current.parent_outcome is not None:
                required = bool(current.parent_outcome)
            else:
                required = not (
                    current.kind == "elseif" and current.chain_index > 0
                )
            result.append((parent, required))
        current = parent
    result.reverse()

    def expand(item: Branch, required: bool,
               visiting: set[tuple[str, bool]]) -> list[tuple[Branch, bool]]:
        key = (item.bid, bool(required))
        if key in visiting:
            return []
        visiting = {*visiting, key}
        expanded: list[tuple[Branch, bool]] = []
        for atom_index, expected in _branch_target_atoms(item, required):
            if not (0 <= atom_index < len(item.atoms)):
                continue
            for guard_branch, guard_required in _local_guard_requirements(
                    ir, item.atoms[atom_index], expected):
                expanded.extend(expand(
                    guard_branch, guard_required, visiting,
                ))
        expanded.append((item, bool(required)))
        return expanded

    result = [item for parent, required in result
              for item in expand(parent, required, set())]
    deduplicated: list[tuple[Branch, bool]] = []
    seen: set[tuple[str, bool]] = set()
    for parent, required in result:
        key = (parent.bid, bool(required))
        if key not in seen:
            seen.add(key)
            deduplicated.append((parent, bool(required)))
    return deduplicated


def _local_guard_requirements(ir: FunctionIR, atom: Any,
                              expected: bool) -> tuple[tuple[Branch, bool], ...]:
    """Resolve the extractor-recorded path for a local truth value."""
    control = next(
        (item for item in ir.control_vars
         if _norm(item.var) == _norm(atom.var)
         or _norm(item.name) == _norm(atom.var)),
        None,
    )
    if control is None or control.source != "local" or atom.boundary is None:
        return ()
    effects = _local_value_effects(ir)
    candidates = []
    for effect in effects:
        if _norm(str(effect.get("name", ""))) != _norm(control.name):
            continue
        value = effect.get("constant_value")
        if value is None:
            continue
        try:
            actual = evaluate_atom(atom, {atom.var: value})
        except (KeyError, TypeError, ValueError):
            continue
        if actual == bool(expected):
            candidates.append(value)
    for value in candidates:
        requirements = _local_value_guard_requirements(ir, atom, value)
        if requirements is not None:
            return requirements
    return ()


def _local_value_guard_requirements(
        ir: FunctionIR, atom: Any, desired_value: Any
        ) -> tuple[tuple[Branch, bool], ...] | None:
    """Return guards that keep a local at one concrete value before an atom."""
    control = next(
        (item for item in ir.control_vars
         if _norm(item.var) == _norm(atom.var)
         or _norm(item.name) == _norm(atom.var)),
        None,
    )
    if control is None or control.source != "local":
        return None
    branches = {item.bid: item for item in ir.branches}
    effects = _local_value_effects(ir)
    before_offset = None
    provenance = getattr(atom, "provenance", None)
    expansion = getattr(provenance, "expansion", None)
    if expansion is not None:
        try:
            before_offset = int(expansion.offset)
        except (TypeError, ValueError):
            before_offset = None
    effects = sorted(
        (item for item in effects
         if _norm(str(item.get("name", ""))) == _norm(control.name)
         and (before_offset is None
              or int(item.get("source_offset", -1)) <= before_offset)),
        key=lambda item: int(item.get("source_offset", -1)),
    )
    selected = next(
        (index for index, effect in enumerate(effects)
         if effect.get("constant_value") == desired_value),
        None,
    )
    if selected is None:
        return None
    requirements: list[tuple[Branch, bool]] = []
    for guard in effects[selected].get("guards", []):
        if not isinstance(guard, dict):
            continue
        branch = branches.get(str(guard.get("bid", "")))
        if branch is not None:
            requirements.append((branch, bool(guard.get("then"))))
    for effect in effects[selected + 1:]:
        if effect.get("constant_value") == desired_value:
            continue
        guards = [item for item in effect.get("guards", [])
                  if isinstance(item, dict)]
        if not guards:
            return None
        guard = guards[0]
        branch = branches.get(str(guard.get("bid", "")))
        if branch is not None:
            requirements.append((branch, not bool(guard.get("then"))))
    return tuple(requirements)


def _apply_branch_target(ir: FunctionIR, domains: dict[str, list[Any]],
                         raw: dict[str, Any], branch: Branch,
                         outcome: bool) -> None:
    """Apply a deterministic truth target without asserting full-path truth."""
    for atom_index, expected in _branch_target_atoms(branch, outcome):
        if atom_index < 0 or atom_index >= len(branch.atoms):
            continue
        atom = branch.atoms[atom_index]
        key = _domain_key_for(ir, atom.var, domains)
        if key is None:
            for guard_branch, guard_required in _local_guard_requirements(
                    ir, atom, expected):
                _apply_branch_target(
                    ir, domains, raw, guard_branch, guard_required,
                )
            continue
        chosen = None
        for value in _targeted_domain_values(ir, branch, key, domains):
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

    if not _apply_switch_path_target(ir, domains, raw, branch):
        return None
    for parent, required in _ancestor_requirements(ir, branch):
        _apply_branch_target(ir, domains, raw, parent, required)
    _apply_branch_target(ir, domains, raw, branch, outcome)

    env = _control_env(raw, ir)
    try:
        return (
            env if branch_path_reachable(ir, branch, env) is True
            and evaluate_branch(branch, env) == outcome else None
        )
    except (KeyError, TypeError, ValueError):
        return None


def _targeted_condition_candidate(ir: FunctionIR,
                                  domains: dict[str, list[Any]],
                                  fixed: dict[str, Any], branch: Branch,
                                  condition_index: int,
                                  outcome: bool) -> dict[str, Any] | None:
    """Construct a witness for one condition while preserving its path."""
    if condition_index < 0 or condition_index >= len(branch.atoms):
        return None
    raw = dict(fixed)
    branch_keys = {
        key for atom in branch.atoms
        if (key := _domain_key_for(ir, atom.var, domains)) is not None
    }
    for key in sorted(domains):
        values = (_targeted_domain_values(ir, branch, key, domains)
                  if key in branch_keys else domains[key])
        if values:
            raw[key] = values[0]
    if not _apply_switch_path_target(ir, domains, raw, branch):
        return None
    for parent, required in _ancestor_requirements(ir, branch):
        _apply_branch_target(ir, domains, raw, parent, required)
    atom = branch.atoms[condition_index]
    key = _domain_key_for(ir, atom.var, domains)
    if key is None:
        for guard_branch, guard_required in _local_guard_requirements(
                ir, atom, bool(outcome)):
            _apply_branch_target(
                ir, domains, raw, guard_branch, guard_required,
            )
        env = _control_env(raw, ir)
        try:
            if (evaluate_atom(atom, env) == outcome
                    and branch_path_reachable(ir, branch, env) is True):
                return env
        except (KeyError, TypeError, ValueError):
            pass
        return None
    for value in _targeted_domain_values(ir, branch, key, domains):
        trial = dict(raw)
        trial[key] = value
        env = _control_env(trial, ir)
        try:
            if (evaluate_atom(atom, env) == outcome
                    and branch_path_reachable(ir, branch, env) is True):
                return env
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _targeted_mcdc_candidate(ir: FunctionIR,
                             domains: dict[str, list[Any]],
                             fixed: dict[str, Any], branch: Branch,
                             condition_index: int,
                             outcome: bool) -> dict[str, Any] | None:
    """Prove MC/DC from the branch/path dimensions only.

    The full input product can contain unrelated global IO columns.  MC/DC
    varies only the branch atoms and their extractor-proven parent path, so
    enumerate that small semantic slice instead of treating the global
    product guard as a proof failure.
    """
    if ((branch.connective or "") not in {"&&", "||"}
            or condition_index < 0
            or condition_index >= len(branch.atoms)):
        return None
    path = _ancestor_requirements(ir, branch)
    expected_truths = _mcdc_expected_truths(
        branch, condition_index, bool(outcome)
    )
    if expected_truths is None:
        return None

    def matches(env: dict[str, Any]) -> bool:
        try:
            atom_values = [evaluate_atom(atom, env) for atom in branch.atoms]
            return (
                all(atom_values[index] is expected
                    for index, expected in expected_truths.items())
                and evaluate_branch(branch, env) is bool(outcome)
                and branch_path_reachable(ir, branch, env) is True
            )
        except (KeyError, TypeError, ValueError):
            return False

    requirements: list[tuple[Branch, Any, bool]] = []
    for parent, required in path:
        for atom_index, expected in _branch_target_atoms(parent, required):
            if 0 <= atom_index < len(parent.atoms):
                requirements.append((parent, parent.atoms[atom_index], expected))
    for index, atom in enumerate(branch.atoms):
        expected = expected_truths.get(index)
        if expected is None:
            return None
        requirements.append((
            branch, atom,
            expected,
        ))

    # First use a linear proof attempt.  Independent atom dimensions never
    # need their Cartesian product; only a conflicting repeated/path control
    # needs the bounded fallback below.
    raw = dict(fixed)
    for key in sorted(domains):
        if domains[key]:
            raw[key] = domains[key][0]
    if not _apply_switch_path_target(ir, domains, raw, branch):
        return None
    applied: list[tuple[Any, bool]] = []
    failed_key: str | None = None
    for owner, atom, expected in requirements:
        key = _domain_key_for(ir, atom.var, domains)
        if key is None:
            # A parent path may be controlled by an extractor-proven constant
            # local.  It is not an input dimension, but it still belongs in
            # the path proof; reject only when its fixed value contradicts
            # the required outcome.
            env = _control_env(raw, ir)
            try:
                if evaluate_atom(atom, env) != expected:
                    return None
            except (KeyError, TypeError, ValueError):
                return None
            applied.append((atom, expected))
            continue
        chosen = None
        for value in _targeted_domain_values(ir, owner, key, domains):
            trial = dict(raw)
            trial[key] = value
            env = _control_env(trial, ir)
            try:
                if (evaluate_atom(atom, env) == expected
                        and all(evaluate_atom(previous, env) == wanted
                                for previous, wanted in applied)):
                    chosen = value
                    break
            except (KeyError, TypeError, ValueError):
                continue
        if chosen is None:
            failed_key = key
            break
        raw[key] = chosen
        applied.append((atom, expected))
    else:
        env = _control_env(raw, ir)
        if matches(env):
            return env

    # Repeated controls in a nested elseif chain can require a different
    # sufficient false witness than the first atom selected above.  Search
    # only that local branch/path slice, with the same deterministic safety
    # bound used for ordinary targeted search.
    relevant: set[str] = set()
    if failed_key is None:
        for _owner, atom, _expected in requirements:
            key = _domain_key_for(ir, atom.var, domains)
            if key is not None:
                relevant.add(key)
    else:
        include = False
        for _owner, atom, _expected in requirements:
            key = _domain_key_for(ir, atom.var, domains)
            if key == failed_key:
                include = True
            if include and key is not None:
                relevant.add(key)
    keys = sorted(relevant)
    values = [domains.get(key, ()) for key in keys]
    cardinality = 1
    for item in values:
        cardinality *= len(item)
    if cardinality > 4096 or any(not item for item in values):
        return None
    for combo in product(*values):
        trial = dict(raw)
        trial.update(dict(zip(keys, combo)))
        env = _control_env(trial, ir)
        if matches(env):
            return env
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
    if obligation.kind == "boundary":
        # Boundary obligations constrain one typed atom; they do not require
        # replaying the full Cartesian product of every unrelated control.
        # Keep the exact requested representative and let the solver's
        # normal branch-path check decide whether the witness is reachable.
        index = obligation.condition_index
        if index is not None and 0 <= index < len(branch.atoms):
            atom = branch.atoms[index]
            key = _domain_key_for(ir, atom.var, domains)
            value = obligation.boundary_value
            control = next(
                (item for item in ir.control_vars
                 if _norm(item.var) == _norm(atom.var)
                 or _norm(item.name) == _norm(atom.var)),
                None,
            )
            if (key is None and control is not None
                    and control.source == "local" and value is not None):
                local_requirements = _local_value_guard_requirements(
                    ir, atom, value,
                )
                if local_requirements is not None:
                    trial = dict(raw)
                    for guard_branch, guard_required in local_requirements:
                        _apply_branch_target(
                            ir, domains, trial, guard_branch, guard_required,
                        )
                    env = _control_env(trial, ir)
                    try:
                        if (branch_path_reachable(ir, branch, env) is True
                                and _lookup(env, atom.var) == value):
                            yield env
                            return
                    except (KeyError, TypeError, ValueError):
                        pass
            if key is not None and value is not None:
                # Array/table index drivers are clipped to the common
                # extractor-proven executable domain in ``_generic_inputs``.
                # A typed scalar boundary outside that domain is not a valid
                # isolated-call witness; do not inject it directly and then
                # report a spurious global-output/evaluator UNKNOWN.
                if value not in domains.get(key, ()):
                    return
                trial = dict(raw)
                if not _apply_switch_path_target(ir, domains, trial, branch):
                    return
                for parent, required in _ancestor_requirements(ir, branch):
                    _apply_branch_target(ir, domains, trial, parent, required)
                trial[key] = value
                env = _control_env(trial, ir)
                try:
                    if branch_path_reachable(ir, branch, env) is True:
                        yield env
                        return
                except (KeyError, TypeError, ValueError):
                    pass
                relevant = {
                    candidate
                    for item in (*_ancestor_requirements(ir, branch),
                                 (branch, True))
                    for atom in item[0].atoms
                    if (candidate := _domain_key_for(ir, atom.var, domains))
                    is not None
                }
                keys = sorted(relevant)
                values = [domains.get(item, ()) for item in keys]
                cardinality = 1
                for item in values:
                    cardinality *= len(item)
                if (values and all(values) and cardinality <= 4096):
                    for combo in product(*values):
                        trial = dict(fixed)
                        trial.update(dict(zip(keys, combo)))
                        trial[key] = value
                        env = _control_env(trial, ir)
                        try:
                            if (branch_path_reachable(ir, branch, env) is True
                                    and _lookup(env, atom.var) == value):
                                yield env
                                return
                        except (KeyError, TypeError, ValueError):
                            continue
        # No typed target could be constructed.  Returning no candidate keeps
        # the result UNKNOWN/UNSAT without rebuilding an infeasible product;
        # the caller retains the explicit solver status and reason.
        return
    if obligation.kind == "condition":
        candidate = _targeted_condition_candidate(
            ir, domains, fixed, branch,
            int(obligation.condition_index), bool(obligation.outcome),
        )
        if candidate is not None:
            yield candidate
        return
    if obligation.kind == "mcdc":
        candidate = _targeted_mcdc_candidate(
            ir, domains, fixed, branch,
            int(obligation.condition_index), bool(obligation.outcome),
        )
        if candidate is not None:
            yield candidate
        return
    if obligation.kind == "case":
        selector = branch.selector
        selector_expression = (
            selector.driver or selector.expression if selector is not None else ""
        )
        key = _domain_key_for(ir, selector_expression, domains)
        if key is None:
            yield _control_env(raw, ir)
            return
        values = domains[key]
        if obligation.boundary_value is not None:
            values = [
                obligation.boundary_value
            ] if obligation.boundary_value in values else []
        for value in values:
            trial = dict(raw)
            trial[key] = value
            env = _control_env(trial, ir)
            try:
                if _switch_obligation_matches(
                        branch, obligation,
                        _switch_selector_value(branch, ir, env)):
                    yield env
                    return
            except (KeyError, TypeError, ValueError):
                continue
        return
    # Large products may use targeted construction only when the C++ Clang
    # extractor preserved the condition AST.  Hand-built/legacy IR without
    # that provenance must retain the original UNSUPPORTED gate instead of
    # guessing a flattened multi-atom expression.
    if branch.condition_tree is None:
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
    from ut_agent.generation.signatures import branch_family, digest

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
        family = branch_family({
            "kind": branch.kind,
            "connective": branch.connective or "single",
            "atoms": atoms,
        })
        result.add(f"family.{digest(family)}")
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
    return GenerationResult(
        ir.name, status, tuple(intents), issues, pack.name,
    )
