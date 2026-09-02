"""Explicit WinAMS projection model.

FunctionIR and TestIntent remain tool-neutral.  This model is the narrow
boundary where the adapter owns target-column names and input/output order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path


_POINTER_BASE = 0x5400
_POINTER_STRIDE = 0x100


def pointer_address(index: int) -> int:
    """Return the deterministic target address used for a pointer input."""
    if index < 0:
        raise ValueError("pointer index must be non-negative")
    return _POINTER_BASE + index * _POINTER_STRIDE


def visible_stub_calls(ir) -> list:
    """Return one target-tool stub record per visible callee."""
    calls = []
    seen: set[str] = set()
    for call in sorted(ir.calls, key=lambda item: item.order):
        callee = (call.callee or "").strip()
        if (not callee or callee in seen
                or call.callee_kind == "memory_helper" or call.ptr_call):
            continue
        seen.add(callee)
        calls.append(call)
    return calls


def stub_capacity(ir, call) -> int:
    """Return the statically proven number of visible call slots."""
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


def stub_param_fields(call, index: int) -> list[str]:
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


def stub_return_fields(call) -> list[str]:
    raw = call.return_fields
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).lstrip(".") for item in raw if str(item).lstrip(".")]


def call_count_key(callee: str) -> str:
    return f"CALLCNT_{callee}"


def qualified_stub_key(callee: str, field: str) -> str:
    return f"AMSTB_SrcFile.c/AMSTB_{callee}@{field}"


def stub_return_keys(callee: str, slot: int, field: str | None = None) -> tuple[str, ...]:
    suffix = f"AMIN_return[{slot}]" + (f".{field}" if field else "")
    return (
        qualified_stub_key(callee, suffix),
        f"AMSTB_{callee}@{suffix}",
        f"{callee}@{suffix}",
        suffix,
    )


def stub_param_keys(callee: str, index: int, slot: int) -> tuple[str, ...]:
    field = f"PTROUT{index:02d}_{callee}[{slot}]"
    return (
        qualified_stub_key(callee, field),
        f"AMSTB_{callee}@{field}",
        f"{callee}@{field}",
    )


def is_stub_return_key(value: str) -> bool:
    return "AMIN_return[" in value


def stub_columns(ir) -> tuple[list[str], list[str]]:
    """Build the adapter-owned input/output names for stub records."""
    inputs: list[str] = []
    outputs: list[str] = []
    for call in visible_stub_calls(ir):
        capacity = stub_capacity(ir, call)
        callee = str(call.callee)
        for index, param in enumerate(call.params):
            slot_name = (
                f"PTROUT{index:02d}_{callee}"
                if param.is_ptr else f"ARG{index:02d}_{callee}"
            )
            fields = stub_param_fields(call, index) if param.is_ptr else []
            names = (
                [f"{slot_name}[{slot}].{field}"
                 for field in fields for slot in range(capacity)]
                if fields else
                [f"{slot_name}[{slot}]" for slot in range(capacity)]
            )
            qualified = [qualified_stub_key(callee, name) for name in names]
            inputs.extend(qualified)
            is_getter = callee.startswith("Rte_Read_") or (
                callee.startswith("pal_") and "_get_" in callee
            )
            if not is_getter:
                is_setter = callee.startswith("pal_") and "_set_" in callee
                info = (call.pointer_arguments.get(str(index), {})
                        if isinstance(call.pointer_arguments, dict) else {})
                if (is_setter and param.is_ptr and isinstance(info, dict)
                        and info.get("is_address") and not info.get("is_null")):
                    outputs.extend(f"{column}[0]" for column in qualified)
                else:
                    outputs.extend(qualified)
        if call.return_used:
            fields = stub_return_fields(call)
            if fields:
                inputs.extend(
                    qualified_stub_key(callee, f"AMIN_return[{slot}].{field}")
                    for field in fields for slot in range(capacity)
                )
            else:
                inputs.extend(
                    qualified_stub_key(callee, f"AMIN_return[{slot}]")
                    for slot in range(capacity)
                )
    return inputs, outputs


def _global_records(ir) -> list[dict]:
    return [asdict(item) for item in ir.global_objects]


def global_object_base(ir, obj: dict) -> str:
    source_file = str(obj.get("source_file") or Path(ir.file).name)
    suffix = Path(source_file).suffix.lower()
    prefix = "" if suffix in {".h", ".hh", ".hpp", ".hxx"} \
        else f"{Path(source_file).name}/"
    if obj.get("is_const") and obj.get("is_volatile"):
        prefix = ""
    return f"{prefix}{obj['name']}"


def global_input_columns(ir) -> list[str]:
    """Expand extractor global-object facts into target input names."""
    columns: list[str] = []
    for obj in _global_records(ir):
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        if not (obj.get("read") or obj.get("write")):
            continue
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
                columns.extend(f"{indexed}.{field}" for field in field_paths)
            else:
                columns.append(indexed)
    return columns


def global_output_columns(ir) -> list[str]:
    """Expand only extractor-proven global write names."""
    columns: list[str] = []
    for obj in _global_records(ir):
        if not isinstance(obj, dict) or not obj.get("name") or not obj.get("write"):
            continue
        if obj.get("is_const") and not obj.get("is_volatile"):
            continue
        base = global_object_base(ir, obj)
        field_paths = [
            str(item).lstrip(".") for item in obj.get("field_paths", ())
            if str(item).lstrip(".")
        ]
        raw_accesses = obj.get("field_accesses", [])
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


@dataclass(frozen=True)
class WinAMSColumn:
    """One rendered target-tool column and its optional semantic lookup key."""

    name: str
    key: str | None = None

    def __iter__(self):
        """Keep existing renderer loops readable at the adapter boundary."""
        yield self.name
        yield self.key


@dataclass(frozen=True)
class WinAMSProjection:
    """The complete ordered target-tool projection for one FunctionIR."""

    inputs: tuple[WinAMSColumn, ...]
    outputs: tuple[WinAMSColumn, ...]

    @classmethod
    def from_pairs(
        cls,
        inputs: list[tuple[str, str | None]],
        outputs: list[tuple[str, str | None]],
    ) -> "WinAMSProjection":
        return cls(
            tuple(WinAMSColumn(*item) for item in inputs),
            tuple(WinAMSColumn(*item) for item in outputs),
        )

    @property
    def comments(self) -> tuple[str, ...]:
        return tuple(item.name for item in (*self.inputs, *self.outputs))
