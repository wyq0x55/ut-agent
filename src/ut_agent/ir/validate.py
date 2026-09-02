"""Public validation boundary for typed FunctionIR values and documents."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .codec import (
    FunctionIRSchemaError,
    function_ir_to_document,
    validate_document,
)
from .model import FunctionIR


def validate_ir(value: FunctionIR | Mapping[str, Any]) -> None:
    """Validate either a typed FunctionIR or its v3 document form."""
    document = function_ir_to_document(value) if isinstance(value, FunctionIR) else value
    validate_document(document)


__all__ = ["FunctionIRSchemaError", "validate_document", "validate_ir"]
