"""Schema and semantic validation for TestBaseline contracts."""
from __future__ import annotations

import json
from pathlib import Path

from .model import TestBaseline


SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "test-baseline.schema.json"


def validate_baseline_mapping(value: dict) -> dict:
    """Validate the original document before model normalization.

    Validating only ``TestBaseline.to_dict()`` would discard unknown YAML
    fields before schema validation and could silently accept a misspelled
    policy.  The source document is therefore checked first.
    """
    try:
        from jsonschema import Draft202012Validator
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(value)
    except FileNotFoundError as exc:
        raise ValueError(f"TestBaseline schema 不存在: {SCHEMA_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"TestBaseline schema 无效: {SCHEMA_PATH}") from exc
    return value


def validate_baseline(baseline: TestBaseline) -> TestBaseline:
    if not baseline.id.strip() or not baseline.version.strip():
        raise ValueError("TestBaseline id/version 不能为空")
    if baseline.status != "approved":
        raise ValueError(
            f"正式生成只接受 approved TestBaseline: {baseline.ref} ({baseline.status})"
        )
    validate_baseline_mapping(baseline.to_dict())
    return baseline
