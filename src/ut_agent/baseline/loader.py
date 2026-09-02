"""Deterministic loader for approved baseline documents."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import TestBaseline
from .validate import validate_baseline, validate_baseline_mapping


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if value in {"{}", "[]"}:
        return {} if value == "{}" else []
    if value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"YAML inline value 无效: {value}") from exc
    if (value.startswith('"') and value.endswith('"')):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"字符串值无效: {value}") from exc
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"[-+]?0[xX][0-9a-fA-F]+", value):
        return int(value, 0)
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", value):
        return float(value)
    return value


def _yaml_subset(text: str) -> dict[str, Any]:
    """Parse the small YAML data subset used by repository contracts.

    This is deliberately a data-only parser: mappings, scalar values and
    inline JSON arrays/objects are supported; executable YAML features are not.
    JSON documents are accepted directly as a strict superset.
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:indent]:
            raise ValueError("YAML contract 不允许 tab 缩进")
        lines.append((indent, raw[indent:]))
    if not lines:
        return {}

    def parse_block(pos: int, indent: int) -> tuple[Any, int]:
        if pos >= len(lines) or lines[pos][0] < indent:
            return {}, pos
        is_list = lines[pos][1].startswith("- ")
        result: Any = [] if is_list else {}
        while pos < len(lines) and lines[pos][0] == indent:
            content = lines[pos][1]
            if is_list:
                if not content.startswith("- "):
                    raise ValueError("YAML mapping/list 缩进混用")
                item = content[2:].strip()
                if ":" in item and not item.startswith(("'", '"')):
                    key, _, tail = item.partition(":")
                    entry: dict[str, Any] = {key.strip(): _scalar(tail)} if tail.strip() else {key.strip(): {}}
                    pos += 1
                    if pos < len(lines) and lines[pos][0] > indent:
                        child, pos = parse_block(pos, lines[pos][0])
                        if not tail.strip():
                            entry[key.strip()] = child
                        elif isinstance(child, dict):
                            entry.update(child)
                    result.append(entry)
                else:
                    result.append(_scalar(item))
                    pos += 1
                continue
            if ":" not in content:
                raise ValueError(f"YAML mapping 行缺少冒号: {content}")
            key, _, tail = content.partition(":")
            key = key.strip()
            if not key:
                raise ValueError("YAML mapping key 不能为空")
            pos += 1
            if tail.strip():
                result[key] = _scalar(tail)
            elif pos < len(lines) and lines[pos][0] > indent:
                result[key], pos = parse_block(pos, lines[pos][0])
            else:
                result[key] = {}
        return result, pos

    result, position = parse_block(0, lines[0][0])
    if position != len(lines) or not isinstance(result, dict):
        raise ValueError("YAML contract 顶层必须是 mapping")
    return result


def load_mapping(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取 contract: {path}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = _yaml_subset(text)
    if not isinstance(value, dict):
        raise ValueError(f"contract 顶层必须是 object: {path}")
    return value


def load_baseline(path: Path) -> TestBaseline:
    raw = load_mapping(Path(path))
    validate_baseline_mapping(raw)
    baseline = TestBaseline.from_mapping(raw)
    return validate_baseline(baseline)
