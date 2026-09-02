"""Project manifest loading and contract validation."""
from __future__ import annotations

import json
from pathlib import Path

from ut_agent.baseline.loader import load_mapping
from .model import ProjectManifest


SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "project-manifest.schema.json"


def load_manifest(path: Path) -> ProjectManifest:
    path = Path(path).resolve()
    raw = load_mapping(path)
    try:
        from jsonschema import Draft202012Validator
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(raw)
    except FileNotFoundError as exc:
        raise ValueError(f"ProjectManifest schema 不存在: {SCHEMA_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"ProjectManifest schema 无效: {SCHEMA_PATH}") from exc
    return ProjectManifest.from_mapping(raw, source_path=str(path))
