"""Compatibility forwarding for candidate inference.

The target-format reader and candidate-pack projection live in the adapter
package.  The deterministic runtime rules engine only consumes an approved
pack and FunctionIR facts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def infer_rule_pack(ir: Any, golden: Path) -> dict[str, Any]:
    from ut_agent.winams.rule_infer import infer_rule_pack as infer

    return infer(ir, golden)


def semantic_csv_signature(path: Path) -> dict[str, Any]:
    from ut_agent.winams.golden import semantic_csv_signature as signature

    return signature(path)


__all__ = ["infer_rule_pack", "semantic_csv_signature"]
