"""Strict FunctionIR v2 JSON validation, conversion, and stable serialization.

The C++ Clang LibTooling extractor is the producer of this document.  Keeping
the contract in a small Python module provides a checked process boundary
without making the Python pipeline infer facts from source text.
"""
from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Mapping

from ut_agent.ir import (
    Atom,
    Branch,
    CallSite,
    Case,
    ControlVar,
    FunctionIR,
    MemoryVar,
    Param,
    Provenance,
    SourceLocation,
    infer_branch_nesting,
)


SCHEMA_VERSION = 2
CONTEXT_SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "docs" / "function-ir-v2.schema.json"


class FunctionIRSchemaError(ValueError):
    """Raised when a FunctionIR v2 document is not structurally valid."""


def _schema() -> dict[str, Any]:
    try:
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FunctionIRSchemaError(f"无法读取 FunctionIR v2 schema: {SCHEMA_PATH}") from exc


@lru_cache(maxsize=1)
def _validator():
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover - packaging error, not data input
        raise FunctionIRSchemaError(
            "FunctionIR v2 adapter 需要 jsonschema>=4.21,<5，请安装项目依赖"
        ) from exc
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _path_text(path: tuple[Any, ...]) -> str:
    return "".join(f"[{item}]" if isinstance(item, int) else f".{item}" for item in path).lstrip(".")


def _semantic_validate(document: Mapping[str, Any]) -> None:
    functions = document["functions"]
    function_names: set[str] = set()
    for function_index, function in enumerate(functions):
        name = function["name"]
        if name in function_names:
            raise FunctionIRSchemaError(f"functions[{function_index}].name 重复: {name}")
        function_names.add(name)

        branch_ids: set[str] = set()
        for branch_index, branch in enumerate(function["branches"]):
            bid = branch["bid"]
            if bid in branch_ids:
                raise FunctionIRSchemaError(
                    f"functions[{function_index}].branches[{branch_index}].bid 重复: {bid}"
                )
            branch_ids.add(bid)
            parent_bid = branch["parent_bid"]
            if parent_bid is not None and parent_bid not in branch_ids:
                raise FunctionIRSchemaError(
                    f"branches[{branch_index}].parent_bid 引用不存在或后置 branch: {parent_bid}"
                )
            for atom in branch["atoms"]:
                _validate_provenance_offsets(atom["provenance"])
            for case in branch["cases"]:
                _validate_provenance_offsets(case["provenance"])
            _validate_provenance_offsets(branch["provenance"])

        call_orders: set[int] = set()
        for call_index, call in enumerate(function["calls"]):
            order = call["order"]
            if order in call_orders:
                raise FunctionIRSchemaError(
                    f"functions[{function_index}].calls[{call_index}].order 重复: {order}"
                )
            call_orders.add(order)
            _validate_provenance_offsets(call["provenance"])

        for control_index, control_var in enumerate(function["control_vars"]):
            for branch_id in control_var["branch_ids"]:
                if branch_id not in branch_ids:
                    raise FunctionIRSchemaError(
                        f"control_vars[{control_index}].branch_ids 引用不存在 branch: {branch_id}"
                    )
            _validate_provenance_offsets(control_var["provenance"])
        for memory in function["memory_vars"]:
            _validate_provenance_offsets(memory["provenance"])
        _validate_provenance_offsets(function["provenance"])


def _validate_provenance_offsets(provenance: Mapping[str, Any]) -> None:
    for location_name in ("spelling", "expansion"):
        location = provenance[location_name]
        if location["end_offset"] < location["offset"]:
            raise FunctionIRSchemaError(
                f"{location_name}.end_offset 不得小于 offset: {location}"
            )


def validate_document(document: Mapping[str, Any]) -> None:
    """Validate a decoded v2 document, including cross-object references."""
    if not isinstance(document, Mapping):
        raise FunctionIRSchemaError("FunctionIR v2 顶层必须是 JSON object")
    errors = sorted(_validator().iter_errors(document), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        location = _path_text(tuple(error.path)) or "<root>"
        raise FunctionIRSchemaError(f"{location}: {error.message}")
    _semantic_validate(document)


def read_document(source: Path) -> dict[str, Any]:
    """Read and strictly validate a v2 JSON file."""
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FunctionIRSchemaError(f"无法读取 FunctionIR v2 JSON: {source}") from exc
    validate_document(data)
    return data


def serialize_document(document: Mapping[str, Any]) -> str:
    """Return deterministic UTF-8-compatible JSON text with one final LF."""
    validate_document(document)
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _location(file: str, line: int) -> dict[str, Any]:
    return {
        "file": file or "<unknown>",
        "line": max(1, int(line or 1)),
        "column": 1,
        "offset": 0,
        "end_offset": 0,
    }


def _provenance(value: Any, file: str, line: int, ast_kind: str) -> tuple[dict[str, Any], bool]:
    if value is not None:
        raw = asdict(value) if isinstance(value, Provenance) else value
        return raw, False
    location = _location(file, line)
    return {
        "spelling": location,
        "expansion": dict(location),
        "macro_stack": [],
        "ast_kind": ast_kind,
    }, True


def _param_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": value["name"],
        "type": value["type"],
        "is_ptr": value["is_ptr"],
        "is_const": value["is_const"],
        "is_written": value["is_written"],
        "extensions": value["extensions"],
    }


def _function_dict(ir: FunctionIR) -> tuple[dict[str, Any], bool]:
    raw = asdict(ir)
    missing_provenance = False
    function_provenance, missing = _provenance(
        ir.provenance, ir.file, ir.line, "FunctionDecl"
    )
    missing_provenance |= missing

    params = [_param_dict(item) for item in raw["params"]]
    branches: list[dict[str, Any]] = []
    for branch in raw["branches"]:
        branch_provenance, missing = _provenance(
            branch["provenance"], branch["file"] or ir.file, branch["line"], "Stmt"
        )
        missing_provenance |= missing
        atoms: list[dict[str, Any]] = []
        for atom in branch["atoms"]:
            atom_provenance, missing = _provenance(
                atom["provenance"], branch["file"] or ir.file, branch["line"], "Expr"
            )
            missing_provenance |= missing
            text = atom["text"]
            atoms.append({
                "var": atom["var"],
                "var_type": atom["var_type"],
                "op": atom["op"],
                "boundary": atom["boundary"],
                "boundary_name": atom["boundary_name"],
                "text": text,
                "mask": atom["mask"],
                "cond_text_spelling": atom["cond_text_spelling"] or text,
                "cond_text_expanded": atom["cond_text_expanded"] or text,
                "type_spelling": atom["type_spelling"] or atom["var_type"],
                "canonical_type": atom["canonical_type"] or atom["var_type"],
                "qualifiers": atom["qualifiers"],
                "provenance": atom_provenance,
                "extensions": atom["extensions"],
            })
        cases: list[dict[str, Any]] = []
        for case in branch["cases"]:
            case_provenance, missing = _provenance(
                case["provenance"], branch["file"] or ir.file, branch["line"], "CaseStmt"
            )
            missing_provenance |= missing
            cases.append({
                "label": case["label"],
                "value": case["value"],
                "is_default": case["is_default"],
                "provenance": case_provenance,
                "extensions": case["extensions"],
            })
        branches.append({
            "bid": branch["bid"],
            "kind": branch["kind"],
            "line": branch["line"],
            "file": branch["file"],
            "cond_text": branch["cond_text"],
            "cond_text_spelling": branch["cond_text_spelling"] or branch["cond_text"],
            "cond_text_expanded": branch["cond_text_expanded"] or branch["cond_text"],
            "atoms": atoms,
            "cases": cases,
            "from_macro": branch["from_macro"],
            "chain_index": branch["chain_index"],
            "connective": branch["connective"],
            "reach_min": branch["reach_min"],
            "reach_max": branch["reach_max"],
            "constant_value": branch["constant_value"],
            "constant_reason": branch["constant_reason"],
            "parent_bid": branch["parent_bid"],
            "provenance": branch_provenance,
            "extensions": branch["extensions"],
        })

    calls: list[dict[str, Any]] = []
    for call in raw["calls"]:
        call_provenance, missing = _provenance(
            call["provenance"], ir.file, call["line"], "CallExpr"
        )
        missing_provenance |= missing
        calls.append({
            "order": call["order"],
            "callee": call["callee"],
            "line": call["line"],
            "via_macro": call["via_macro"],
            "ptr_call": call["ptr_call"],
            "is_static": call["is_static"],
            "table_base": call["table_base"],
            "table_member": call["table_member"],
            "arg_types": call["arg_types"],
            "params": [_param_dict(item) for item in call["params"]],
            "ret_type": call["ret_type"],
            "provenance": call_provenance,
            "extensions": call["extensions"],
        })

    controls: list[dict[str, Any]] = []
    for control in raw["control_vars"]:
        control_provenance, missing = _provenance(
            control["provenance"], ir.file, ir.line, "ControlVar"
        )
        missing_provenance |= missing
        controls.append({**control, "provenance": control_provenance})

    memories: list[dict[str, Any]] = []
    for memory in raw["memory_vars"]:
        memory_provenance, missing = _provenance(
            memory["provenance"], ir.file, ir.line, "MemoryAccess"
        )
        missing_provenance |= missing
        memories.append({**memory, "provenance": memory_provenance})

    function = {
        "name": raw["name"],
        "file": raw["file"],
        "line": raw["line"],
        "line_end": max(raw["line_end"], raw["line"], 1),
        "ret_type": raw["ret_type"],
        "params": params,
        "globals_used": raw["globals_used"],
        "locals": raw["locals"],
        "calls": calls,
        "branches": branches,
        "config": raw["config"],
        "notes": raw["notes"],
        "enums": raw["enums"],
        "global_writes": raw["global_writes"],
        "control_vars": controls,
        "config_ptrs": raw["config_ptrs"],
        "memory_vars": memories,
        "status": "PARTIAL" if missing_provenance and raw["status"] == "OK" else raw["status"],
        "provenance": function_provenance,
        "diagnostics": raw["diagnostics"],
        "extensions": raw["extensions"],
    }
    return function, missing_provenance


def _default_context(ir: FunctionIR) -> dict[str, Any]:
    config = {str(key): str(value) for key, value in ir.config.items()}
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "language": "c",
        "standard": "c99",
        "source_files": [str(Path(ir.file).resolve())],
        "include_dirs": [],
        "defines": config,
        "force_includes": [],
        "target_triple": None,
        "cpu": None,
        "abi": None,
        "sysroot": None,
        "resource_dir": None,
        "extra_args": [],
    }


def function_ir_to_document(ir: FunctionIR) -> dict[str, Any]:
    """Wrap one FunctionIR in the complete v2 document shape."""
    function, missing_provenance = _function_dict(ir)
    status = ir.status
    diagnostics = list(ir.diagnostics)
    if missing_provenance:
        status = "PARTIAL" if status == "OK" else status
        diagnostics.append({
            "code": "LEGACY_PROVENANCE_APPROXIMATION",
            "severity": "warning",
            "message": "当前 FunctionIR 缺少完整 source range；v2 输出使用函数/语句行作为兼容证据。",
            "related_ids": [function["name"]],
        })
    context = ir.compile_context or _default_context(ir)
    document = {
        "schema_version": SCHEMA_VERSION,
        "extractor": dict(ir.extractor),
        "status": status,
        "compile_context": context,
        "diagnostics": diagnostics,
        "functions": [function],
    }
    validate_document(document)
    return document


def _source_location(value: Mapping[str, Any]) -> SourceLocation:
    return SourceLocation(
        file=value["file"], line=value["line"], column=value["column"],
        offset=value["offset"], end_offset=value["end_offset"],
    )


def _read_provenance(value: Mapping[str, Any]) -> Provenance:
    return Provenance(
        spelling=_source_location(value["spelling"]),
        expansion=_source_location(value["expansion"]),
        macro_stack=list(value["macro_stack"]),
        ast_kind=value["ast_kind"],
    )


def _read_param(value: Mapping[str, Any]) -> Param:
    return Param(
        name=value["name"], type=value["type"], is_ptr=value["is_ptr"],
        is_const=value["is_const"], is_written=value["is_written"],
        extensions=dict(value["extensions"]),
    )


def document_to_function_ir(document: Mapping[str, Any]) -> FunctionIR:
    """Validate a v2 document and map its single function to FunctionIR."""
    validate_document(document)
    functions = document["functions"]
    if len(functions) != 1:
        raise FunctionIRSchemaError(
            f"FunctionIR adapter 只接受单函数文档，实际 functions={len(functions)}"
        )
    value = functions[0]
    branches: list[Branch] = []
    for branch in value["branches"]:
        atoms = [Atom(
            var=atom["var"], var_type=atom["var_type"], op=atom["op"],
            boundary=atom["boundary"], boundary_name=atom["boundary_name"],
            text=atom["text"], mask=atom["mask"],
            cond_text_spelling=atom["cond_text_spelling"],
            cond_text_expanded=atom["cond_text_expanded"],
            type_spelling=atom["type_spelling"], canonical_type=atom["canonical_type"],
            qualifiers=list(atom["qualifiers"]),
            provenance=_read_provenance(atom["provenance"]),
            extensions=dict(atom["extensions"]),
        ) for atom in branch["atoms"]]
        cases = [Case(
            label=case["label"], value=case["value"], is_default=case["is_default"],
            provenance=_read_provenance(case["provenance"]),
            extensions=dict(case["extensions"]),
        ) for case in branch["cases"]]
        branches.append(Branch(
            bid=branch["bid"], kind=branch["kind"], line=branch["line"],
            file=branch["file"], cond_text=branch["cond_text"], atoms=atoms,
            cases=cases, from_macro=branch["from_macro"], chain_index=branch["chain_index"],
            connective=branch["connective"], reach_min=branch["reach_min"],
            reach_max=branch["reach_max"], constant_value=branch["constant_value"],
            constant_reason=branch["constant_reason"],
            cond_text_spelling=branch["cond_text_spelling"],
            cond_text_expanded=branch["cond_text_expanded"], parent_bid=branch["parent_bid"],
            provenance=_read_provenance(branch["provenance"]),
            extensions=dict(branch["extensions"]),
        ))
    infer_branch_nesting(branches)
    calls = [CallSite(
        order=call["order"], callee=call["callee"], line=call["line"],
        via_macro=call["via_macro"], ptr_call=call["ptr_call"], is_static=call["is_static"],
        table_base=call["table_base"], table_member=call["table_member"],
        arg_types=list(call["arg_types"]), params=[_read_param(item) for item in call["params"]],
        ret_type=call["ret_type"], provenance=_read_provenance(call["provenance"]),
        extensions=dict(call["extensions"]),
    ) for call in value["calls"]]
    controls = [ControlVar(
        name=item["name"], var=item["var"], source=item["source"], set_via=item["set_via"],
        var_type=item["var_type"], constant_value=item["constant_value"],
        constant_reason=item["constant_reason"], branch_ids=list(item["branch_ids"]),
        provenance=_read_provenance(item["provenance"]), extensions=dict(item["extensions"]),
    ) for item in value["control_vars"]]
    memories = [MemoryVar(
        name=item["name"], address=item["address"], width=item["width"], read=item["read"],
        write=item["write"], conditional=item["conditional"], input_value=item["input_value"],
        expected_value=item["expected_value"], provenance=_read_provenance(item["provenance"]),
        extensions=dict(item["extensions"]),
    ) for item in value["memory_vars"]]
    return FunctionIR(
        name=value["name"], file=value["file"], line=value["line"],
        ret_type=value["ret_type"], line_end=value["line_end"],
        params=[_read_param(item) for item in value["params"]],
        globals_used=list(value["globals_used"]), locals=list(value["locals"]), calls=calls,
        branches=branches, config=dict(value["config"]), notes=list(value["notes"]),
        enums={key: dict(item) for key, item in value["enums"].items()},
        global_writes=list(value["global_writes"]), control_vars=controls,
        config_ptrs=list(value["config_ptrs"]), memory_vars=memories,
        status=value["status"], provenance=_read_provenance(value["provenance"]),
        diagnostics=list(value["diagnostics"]), compile_context=dict(document["compile_context"]),
        extractor=dict(document["extractor"]), extensions=dict(value["extensions"]),
    )
