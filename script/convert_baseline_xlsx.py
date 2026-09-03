#!/usr/bin/env python3
"""Convert an XLSX baseline document into reviewable Markdown and source YAML.

The conversion is intentionally a cell-preserving transcription.  It does not
infer test rules, normalize Japanese text, or turn spreadsheet prose into an
approved executable baseline.  Each populated cell keeps its sheet, address,
cell kind, and source value; formulas also keep their formula and cached value.

The script uses only the Python standard library so it can run with the
workspace runtime without adding a spreadsheet dependency to ut-agent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _read_text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    return "".join(element.itertext())


def _visible_text(element: ET.Element) -> str:
    """Read visible rich-text runs while excluding Excel phonetic runs."""
    phonetic = _tag(MAIN, "rPh")
    text_tag = _tag(MAIN, "t")
    chunks: list[str] = []

    def visit(node: ET.Element, in_phonetic: bool = False) -> None:
        now_phonetic = in_phonetic or node.tag == phonetic
        if node.tag == text_tag and not now_phonetic:
            chunks.append(node.text or "")
        for child in node:
            visit(child, now_phonetic)

    visit(element)
    return "".join(chunks)


def _cell_column(address: str) -> int:
    match = re.match(r"([A-Z]+)", address.upper())
    if not match:
        return 0
    value = 0
    for char in match.group(1):
        value = value * 26 + ord(char) - ord("A") + 1
    return value


def _cell_row(address: str) -> int:
    match = re.search(r"(\d+)$", address)
    return int(match.group(1)) if match else 0


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _target_part(target: str) -> str:
    target = target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        data = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    return [_visible_text(item) for item in root.findall(_tag(MAIN, "si"))]


def _workbook_parts(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        item.attrib["Id"]: _target_part(item.attrib["Target"])
        for item in relationships.findall(_tag(PKG_REL, "Relationship"))
    }
    sheets = workbook.find(_tag(MAIN, "sheets"))
    if sheets is None:
        return []
    result: list[dict[str, Any]] = []
    for sheet in sheets.findall(_tag(MAIN, "sheet")):
        rel_id = sheet.attrib.get(_tag(DOC_REL, "id"), "")
        result.append({
            "name": sheet.attrib.get("name", ""),
            "state": sheet.attrib.get("state", "visible"),
            "xml_part": rel_targets.get(rel_id, ""),
        })
    return result


def _cell_record(cell: ET.Element, shared: list[str]) -> dict[str, Any] | None:
    address = cell.attrib.get("r", "")
    cell_type = cell.attrib.get("t", "n")
    formula_node = cell.find(_tag(MAIN, "f"))
    value_node = cell.find(_tag(MAIN, "v"))
    inline_node = cell.find(_tag(MAIN, "is"))
    formula = _read_text(formula_node)
    raw_value = _read_text(value_node)

    if inline_node is not None:
        value = _visible_text(inline_node)
        kind = "inline_string"
    elif cell_type == "s" and raw_value is not None:
        index = int(raw_value)
        value = shared[index] if 0 <= index < len(shared) else raw_value
        kind = "shared_string"
    elif cell_type == "b":
        value = raw_value
        kind = "boolean"
    elif cell_type == "e":
        value = raw_value
        kind = "error"
    elif cell_type == "str":
        value = raw_value
        kind = "string"
    else:
        value = raw_value
        kind = "number"

    if formula is not None:
        kind = "formula"
    if value is None and formula is None:
        return None

    record: dict[str, Any] = {
        "coordinate": address,
        "row": _cell_row(address),
        "column": _cell_column(address),
        "cell_type": kind,
        "value": value if value is not None else "",
    }
    if formula is not None:
        record["formula"] = formula
        record["cached_value"] = raw_value
    return record


def _sheet_record(archive: zipfile.ZipFile, part: dict[str, Any], shared: list[str]) -> dict[str, Any]:
    root = ET.fromstring(archive.read(part["xml_part"]))
    dimension_node = root.find(_tag(MAIN, "dimension"))
    merged = root.find(_tag(MAIN, "mergeCells"))
    merged_ranges = [
        node.attrib.get("ref", "")
        for node in (merged.findall(_tag(MAIN, "mergeCell")) if merged is not None else [])
    ]
    cells: list[dict[str, Any]] = []
    sheet_data = root.find(_tag(MAIN, "sheetData"))
    if sheet_data is not None:
        for row in sheet_data.findall(_tag(MAIN, "row")):
            for cell in row.findall(_tag(MAIN, "c")):
                record = _cell_record(cell, shared)
                if record is not None:
                    cells.append(record)
    cells.sort(key=lambda item: (item["row"], item["column"], item["coordinate"]))
    max_row = max((item["row"] for item in cells), default=0)
    max_column = max((item["column"] for item in cells), default=0)
    return {
        "name": part["name"],
        "state": part["state"],
        "xml_part": part["xml_part"],
        "dimension": dimension_node.attrib.get("ref", "") if dimension_node is not None else "",
        "max_row": max_row,
        "max_column": max_column,
        "merged_ranges": merged_ranges,
        "nonempty_cell_count": len(cells),
        "cells": cells,
    }


def convert(input_path: Path, repo_root: Path) -> dict[str, Any]:
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(input_path) as archive:
        shared = _shared_strings(archive)
        sheets = [_sheet_record(archive, part, shared) for part in _workbook_parts(archive)]
    return {
        "format_version": 1,
        "kind": "xlsx-source-transcription",
        "source": {
            "file": _repo_relative(input_path, repo_root),
            "sha256": digest,
            "format": "xlsx",
            "conversion": "deterministic cell-preserving transcription",
            "semantic_status": "source_only",
            "note": "This document records workbook evidence; it is not an approved executable baseline.",
        },
        "workbook": {
            "sheet_count": len(sheets),
            "sheets": sheets,
        },
    }


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _yaml_lines(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                if not child:
                    lines.append(f"{prefix}{key}: {_yaml_scalar(child)}")
                else:
                    lines.append(f"{prefix}{key}:")
                    lines.extend(_yaml_lines(child, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(child)}")
        return lines
    if isinstance(value, list):
        lines = []
        for child in value:
            if isinstance(child, dict):
                if not child:
                    lines.append(f"{prefix}- {{}}")
                    continue
                first = True
                for key, item in child.items():
                    if isinstance(item, (dict, list)):
                        if not item:
                            line = f"{prefix}- {key}: {_yaml_scalar(item)}" if first else f"{' ' * (indent + 2)}{key}: {_yaml_scalar(item)}"
                            lines.append(line)
                        else:
                            line = f"{prefix}- {key}:" if first else f"{' ' * (indent + 2)}{key}:"
                            lines.append(line)
                            lines.extend(_yaml_lines(item, indent + (2 if first else 4)))
                    else:
                        line = f"{prefix}- {key}: {_yaml_scalar(item)}" if first else f"{' ' * (indent + 2)}{key}: {_yaml_scalar(item)}"
                        lines.append(line)
                    first = False
            elif isinstance(child, list):
                lines.append(f"{prefix}-")
                lines.extend(_yaml_lines(child, indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(child)}")
        return lines
    return [f"{prefix}{_yaml_scalar(value)}"]


def _markdown_value(value: Any) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def _markdown(data: dict[str, Any]) -> str:
    source = data["source"]
    sheets = data["workbook"]["sheets"]
    lines = [
        "# 基準単体テスト項目基準書 Ver.1.6（原表転記）",
        "",
        "> 本文書は XLSX の populated cell を座標付きで転記した review 用資料です。",
        "> 内容からテスト規則を推測したり、承認済みの実行基線へ自動昇格したりしません。",
        "",
        "## Source",
        "",
        f"- File: `{source['file']}`",
        f"- SHA-256: `{source['sha256']}`",
        f"- Conversion: {source['conversion']}",
        f"- Semantic status: `{source['semantic_status']}`",
        "",
        "## Workbook structure",
        "",
        "| Sheet | Dimension | Non-empty cells | Merged ranges | State |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for sheet in sheets:
        lines.append(
            f"| `{sheet['name']}` | `{sheet['dimension']}` | "
            f"{sheet['nonempty_cell_count']} | {len(sheet['merged_ranges'])} | {sheet['state']} |"
        )
    for sheet in sheets:
        lines.extend(["", f"## Sheet: `{sheet['name']}`", ""])
        lines.append(
            f"Dimension `{sheet['dimension']}`; {sheet['nonempty_cell_count']} populated cells; "
            f"XML part `{sheet['xml_part']}`."
        )
        if sheet["merged_ranges"]:
            lines.append("")
            lines.append("Merged ranges: " + ", ".join(f"`{item}`" for item in sheet["merged_ranges"]) + ".")
        by_row: dict[int, list[dict[str, Any]]] = {}
        for cell in sheet["cells"]:
            by_row.setdefault(cell["row"], []).append(cell)
        for row_number, cells in by_row.items():
            lines.extend(["", f"### Row {row_number}", ""])
            for cell in cells:
                extra = ""
                if "formula" in cell:
                    extra = f"; formula={_markdown_value(cell['formula'])}; cached={_markdown_value(cell['cached_value'])}"
                lines.append(
                    f"- `{cell['coordinate']}` ({cell['cell_type']}): "
                    f"{_markdown_value(cell['value'])}{extra}"
                )
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source .xlsx file")
    parser.add_argument("--markdown", type=Path, required=True, help="output Markdown path")
    parser.add_argument("--yaml", dest="yaml_path", type=Path, required=True, help="output YAML path")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="root used for portable source paths")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    input_path = args.input.resolve()
    if not input_path.is_file():
        raise SystemExit(f"input XLSX not found: {input_path}")
    data = convert(input_path, args.repo_root.resolve())
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.yaml_path.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(_markdown(data), encoding="utf-8", newline="\n")
    args.yaml_path.write_text("\n".join(_yaml_lines(data)) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "source": data["source"],
        "sheet_count": data["workbook"]["sheet_count"],
        "sheets": [
            {
                "name": sheet["name"],
                "dimension": sheet["dimension"],
                "nonempty_cell_count": sheet["nonempty_cell_count"],
            }
            for sheet in data["workbook"]["sheets"]
        ],
        "markdown": args.markdown.as_posix(),
        "yaml": args.yaml_path.as_posix(),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
