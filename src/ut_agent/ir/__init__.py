"""Public FunctionIR model and its checked JSON contract adapter."""

from .model import (
    Atom,
    Branch,
    CallSite,
    Case,
    ControlVar,
    Effect,
    FieldAccess,
    FunctionIR,
    GlobalObject,
    MemoryVar,
    Param,
    Provenance,
    RecordLayoutField,
    SourceLocation,
    TypeInfo,
    ValueOrigin,
)
from .codec import (
    CONTEXT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    FunctionIRSchemaError,
    document_to_function_ir,
    function_ir_to_document,
    read_document,
    serialize_document,
    validate_document,
)
from .validate import validate_ir

__all__ = [
    "Atom", "Branch", "CallSite", "Case", "ControlVar", "Effect",
    "FieldAccess", "FunctionIR", "GlobalObject", "MemoryVar", "Param",
    "Provenance", "RecordLayoutField", "SourceLocation", "TypeInfo",
    "ValueOrigin", "CONTEXT_SCHEMA_VERSION", "SCHEMA_VERSION",
    "FunctionIRSchemaError", "document_to_function_ir", "function_ir_to_document",
    "read_document", "serialize_document", "validate_document",
    "validate_ir",
]
