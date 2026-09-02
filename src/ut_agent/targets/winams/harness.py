"""Typed WinAMS harness planning boundary.

The planner records how a semantic suite can be materialized for WinAMS.  It
does not choose testcase values, evaluate C, or read a Golden CSV.  In
particular, pointer objects are described by stable relations; concrete target
addresses remain an adapter/materialization concern.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ut_agent.generation.semantic import (
    call_columns, global_input_columns, global_output_columns,
    pointer_address_key, pointer_value_key,
)
from ut_agent.ir import FunctionIR


@dataclass(frozen=True)
class FunctionExecutionSchema:
    function: str
    input_symbols: tuple[str, ...]
    output_symbols: tuple[str, ...]
    fixed_schema: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CallSequencePlan:
    callee: str
    orders: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StubBindingPlan:
    callee: str
    call_orders: tuple[int, ...]
    return_symbols: tuple[str, ...] = ()
    pointer_output_symbols: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryFixturePlan:
    object_id: str
    pointer_symbol: str
    relation: str
    pointee_type: str = ""
    element_count: int = 1
    address: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InputOutputBindingPlan:
    input_symbols: tuple[str, ...]
    output_symbols: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HarnessPlan:
    execution_schema: FunctionExecutionSchema
    call_sequence: tuple[CallSequencePlan, ...]
    stubs: tuple[StubBindingPlan, ...]
    memory: tuple[MemoryFixturePlan, ...]
    bindings: InputOutputBindingPlan

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_schema": self.execution_schema.to_dict(),
            "call_sequence": [item.to_dict() for item in self.call_sequence],
            "stubs": [item.to_dict() for item in self.stubs],
            "memory": [item.to_dict() for item in self.memory],
            "bindings": self.bindings.to_dict(),
        }


def plan_harness(ir: FunctionIR) -> HarnessPlan:
    """Build a deterministic target plan from typed FunctionIR facts."""
    semantic_call_inputs, semantic_call_outputs = call_columns(ir)
    inputs: list[str] = [param.name for param in ir.params]
    inputs.extend(pointer_address_key(param.name) for param in ir.params if param.is_ptr)
    inputs.extend(pointer_value_key(param.name) for param in ir.params if param.is_ptr)
    inputs.extend(global_input_columns(ir))
    inputs.extend(semantic_call_inputs)
    outputs: list[str] = []
    if ir.ret_type not in ("", "void"):
        outputs.append("return")
    outputs.extend(
        pointer_value_key(param.name)
        for param in ir.params if param.is_ptr and param.is_written
    )
    outputs.extend(global_output_columns(ir))
    outputs.extend(semantic_call_outputs)

    def unique(values: list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(item) for item in values if str(item)))

    visible = [
        call for call in sorted(ir.calls, key=lambda item: item.order)
        if call.callee and call.callee_kind != "memory_helper" and not call.ptr_call
    ]
    grouped: dict[str, list[Any]] = {}
    for call in visible:
        grouped.setdefault(str(call.callee), []).append(call)
    sequence = tuple(
        CallSequencePlan(callee, tuple(int(item.order) for item in entries))
        for callee, entries in sorted(grouped.items())
    )
    stubs = tuple(
        StubBindingPlan(
            callee=callee,
            call_orders=item.orders,
            return_symbols=tuple(
                f"call:{callee}:return:{slot}"
                for slot in range(max(1, len(item.orders)))
                if any(call.return_used for call in grouped[callee])
            ),
            pointer_output_symbols=tuple(
                f"call:{callee}:param:{index}:{slot}.pointee"
                for call in grouped[callee]
                for index, param in enumerate(call.params)
                if param.is_ptr and param.is_written
                for slot in range(max(1, len(item.orders)))
            ),
        )
        for item in sequence
        for callee in (item.callee,)
    )
    memory = tuple(
        MemoryFixturePlan(
            object_id=f"object:{param.name}",
            pointer_symbol=param.name,
            relation="non-null" if param.is_ptr else "none",
            pointee_type=(param.type_info.pointee_type
                          if param.type_info and param.type_info.pointee_type
                          else ""),
        )
        for param in ir.params if param.is_ptr
    )
    schema = FunctionExecutionSchema(
        function=ir.name,
        input_symbols=unique(inputs),
        output_symbols=unique(outputs),
    )
    return HarnessPlan(
        execution_schema=schema,
        call_sequence=sequence,
        stubs=stubs,
        memory=memory,
        bindings=InputOutputBindingPlan(schema.input_symbols, schema.output_symbols),
    )


__all__ = [
    "CallSequencePlan", "FunctionExecutionSchema", "HarnessPlan",
    "InputOutputBindingPlan", "MemoryFixturePlan", "StubBindingPlan",
    "plan_harness",
]
