"""Tool-neutral semantic identifiers used by the deterministic rule engine.

The rules layer describes observations in terms of the extracted call,
parameter, pointer, and global facts.  Target-tool column spellings belong to
an adapter and must not leak back into this module.
"""
from __future__ import annotations

from dataclasses import asdict
from itertools import product
from typing import Any


def _records(ir) -> list[dict[str, Any]]:
    return [asdict(item) for item in ir.global_objects]


def _field_list(call, index: int) -> list[str]:
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


def call_count_key(callee: str) -> str:
    return f"call:{callee}:count"


def call_return_key(callee: str, slot: int, field: str | None = None) -> str:
    suffix = f"call:{callee}:return:{slot}"
    return f"{suffix}.{field}" if field else suffix


def call_return_keys(callee: str, slot: int, field: str | None = None) -> tuple[str, ...]:
    return (call_return_key(callee, slot, field),)


def call_param_key(
    callee: str, index: int, slot: int, field: str | None = None,
) -> str:
    suffix = f"call:{callee}:param:{index}:{slot}"
    return f"{suffix}.{field}" if field else suffix


def call_param_keys(callee: str, index: int, slot: int) -> tuple[str, ...]:
    return (call_param_key(callee, index, slot),)


def is_call_return_key(value: str) -> bool:
    return str(value).startswith("call:") and ":return:" in str(value)


def pointer_address_key(name: str) -> str:
    return f"param:{name}:address"


def pointer_value_key(name: str, path: str | None = None) -> str:
    compact = str(path or "").strip().lstrip("@*").lstrip(".")
    if compact in {"", name}:
        compact = "value"
    return f"param:{name}:pointee:{compact}"


def global_key(name: str, indexes: tuple[int, ...] = (), field: str | None = None) -> str:
    value = f"global:{name}" + "".join(f"[{index}]" for index in indexes)
    return f"{value}.{field}" if field else value


def global_base_key(name: str) -> str:
    return global_key(name)


def visible_calls(ir) -> list:
    calls = []
    seen: set[str] = set()
    for call in sorted(ir.calls, key=lambda item: item.order):
        callee = (call.callee or "").strip()
        if not callee or callee in seen or call.callee_kind == "memory_helper" or call.ptr_call:
            continue
        seen.add(callee)
        calls.append(call)
    return calls


def call_capacity(ir, call) -> int:
    if getattr(call, "via_macro", None):
        return 1
    occurrences = [
        item for item in ir.calls
        if (item.callee or "").strip() == (call.callee or "").strip()
        and item.callee_kind != "memory_helper" and not item.ptr_call
    ]
    total = 0
    for item in occurrences:
        try:
            total += max(1, int(item.max_occurrences))
        except (TypeError, ValueError):
            total += 1
    return max(1, total)


def return_fields(call) -> list[str]:
    raw = call.return_fields
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).lstrip(".") for item in raw if str(item).lstrip(".")]


def param_fields(call, index: int) -> list[str]:
    return _field_list(call, index)


def param_columns(ir, call, index: int) -> list[str]:
    capacity = call_capacity(ir, call)
    fields = param_fields(call, index) if call.params[index].is_ptr else []
    columns = [
        call_param_key(str(call.callee), index, slot, field)
        for field in fields for slot in range(capacity)
    ] or [
        call_param_key(str(call.callee), index, slot)
        for slot in range(capacity)
    ]
    info = call.pointer_arguments.get(str(index), {}) \
        if isinstance(call.pointer_arguments, dict) else {}
    if (call.params[index].is_ptr and isinstance(info, dict)
            and info.get("address_used") and not info.get("nullable")
            and info.get("pointee_write")):
        columns.extend(
            call_param_key(str(call.callee), index, slot, "pointee")
            for slot in range(capacity)
        )
    return columns


def return_columns(ir, call) -> list[str]:
    if not call.return_used:
        return []
    fields = return_fields(call)
    capacity = call_capacity(ir, call)
    return [
        call_return_key(str(call.callee), slot, field)
        for field in fields for slot in range(capacity)
    ] or [call_return_key(str(call.callee), slot) for slot in range(capacity)]


def output_columns(ir, call) -> list[str]:
    columns: list[str] = []
    for index, param in enumerate(call.params):
        names = param_columns(ir, call, index)
        if not param.is_ptr:
            columns.extend(names)
            continue
        observable = call.caller_param_output.get(
            str(index), call.caller_param_output.get(index, False)
        ) if isinstance(call.caller_param_output, dict) else False
        if observable:
            info = call.pointer_arguments.get(str(index), {}) \
                if isinstance(call.pointer_arguments, dict) else {}
            if (isinstance(info, dict) and info.get("address_used", info.get("is_address"))
                    and not info.get("nullable", info.get("is_null"))
                    and info.get("pointee_write")):
                columns.extend(
                    call_param_key(str(call.callee), index, slot, "pointee")
                    for slot in range(call_capacity(ir, call))
                )
            else:
                columns.extend(names)
    return columns


def global_object_columns(ir, obj: dict[str, Any], *, writable: bool) -> list[str]:
    return _global_columns(ir, obj, writable=writable)


def _global_columns(ir, obj: dict[str, Any], *, writable: bool) -> list[str]:
    if not obj.get("name") or (not obj.get("write") if writable else not (obj.get("read") or obj.get("write"))):
        return []
    if obj.get("is_const") and not obj.get("is_volatile"):
        return []
    field_paths = [
        str(item).lstrip(".") for item in obj.get("field_paths", ())
        if str(item).lstrip(".")
    ]
    raw_accesses = obj.get("field_accesses", [])
    accesses = {
        str(item.get("path", "")).lstrip("."): (
            bool(item.get("read")), bool(item.get("write"))
        )
        for item in raw_accesses if isinstance(item, dict) and item.get("path")
    } if isinstance(raw_accesses, list) else {}
    if writable and field_paths and accesses:
        field_paths = [
            path for path in field_paths
            if any(
                (read or write)
                and (path == access or path.startswith(access + ".")
                     or access.startswith(path + "."))
                for access, (read, write) in accesses.items()
            )
        ]
        layout = obj.get("record_layout", [])
        if isinstance(layout, list):
            def accessed(path: str) -> bool:
                return any(
                    access == path or access.startswith(path + ".")
                    or path.startswith(access + ".")
                    for access in accesses
                )

            accessed_bitfields = {
                str(item.get("path", "")).lstrip(".")
                for item in layout
                if isinstance(item, dict) and item.get("is_bitfield")
                and accessed(str(item.get("path", "")).lstrip("."))
            }
            for item in layout:
                if not isinstance(item, dict) or item.get("is_bitfield"):
                    continue
                storage = str(item.get("path", "")).lstrip(".")
                if not storage or storage in field_paths or not accessed_bitfields:
                    continue
                try:
                    start = int(item["bit_offset"])
                    width = int(item["bit_width"])
                except (KeyError, TypeError, ValueError):
                    continue
                if any(
                    int(bit.get("bit_offset", -1)) >= start
                    and int(bit.get("bit_offset", -1))
                    + int(bit.get("bit_width", 0)) <= start + width
                    for bit in layout if isinstance(bit, dict)
                    and bit.get("is_bitfield")
                ):
                    field_paths.append(storage)
    sizes: list[int] = []
    for raw_size in obj.get("array_sizes", ()):
        try:
            sizes.append(max(0, int(raw_size)))
        except (TypeError, ValueError):
            sizes = []
            break
    if any(size == 0 for size in sizes):
        return []
    indexes = list(product(*(range(size) for size in sizes))) if sizes else [()]
    result: list[str] = []
    for index in indexes:
        if field_paths:
            result.extend(
                global_key(str(obj["name"]), tuple(index), field)
                for field in field_paths
            )
        else:
            result.append(global_key(str(obj["name"]), tuple(index)))
    return result


def global_input_columns(ir) -> list[str]:
    return [
        column for obj in _records(ir)
        if isinstance(obj, dict)
        for column in _global_columns(ir, obj, writable=False)
    ]


def global_output_columns(ir) -> list[str]:
    return [
        column for obj in _records(ir)
        if isinstance(obj, dict)
        for column in _global_columns(ir, obj, writable=True)
    ]


def call_columns(ir) -> tuple[list[str], list[str]]:
    """Return semantic call inputs and caller-observable outputs."""
    inputs: list[str] = []
    outputs: list[str] = []
    for call in visible_calls(ir):
        for index, param in enumerate(call.params):
            names = param_columns(ir, call, index)
            inputs.extend(names)
        outputs.extend(output_columns(ir, call))
        if call.return_used:
            inputs.extend(return_columns(ir, call))
    return inputs, outputs


__all__ = [
    "call_capacity", "call_columns", "call_count_key", "call_param_key",
    "call_param_keys", "call_return_key", "call_return_keys", "global_object_columns",
    "global_base_key", "global_input_columns", "global_key",
    "global_output_columns", "is_call_return_key", "pointer_address_key",
    "pointer_value_key", "param_columns", "param_fields", "return_columns",
    "return_fields", "output_columns", "visible_calls",
]
