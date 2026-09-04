#!/usr/bin/env python3
"""Build an evidence-only rule corpus from existing Typed FunctionIR output.

Input is a generated-output tree containing ``function-ir.json`` files and a
matching WinAMS Golden root containing ``<source>.c/<function>/TestCsv/*.csv``.
The tool only decodes FunctionIR through the v3 codec and records Golden paths;
it never reads C source text or derives semantics from CSV values.  Output is
written to the requested JSON paths under ``.tmp`` by the caller.  The input
trees are never modified and generated JSON can be removed with the rest of
the caller's temporary run directory.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ut_agent.ir.codec import document_to_function_ir
from ut_agent.learning.corpus import semantic_pattern


def _golden_samples(golden_root: Path, source_root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for csv_path in sorted(
        (
            path for path in golden_root.rglob("*.csv")
            if path.parent.name.casefold() == "testcsv"
        ),
        key=lambda path: path.as_posix().casefold(),
    ):
        function_dir = csv_path.parent.parent
        source_container = function_dir.parent
        source_rel = source_container.relative_to(golden_root)
        source = (source_root / source_rel).resolve()
        result.append({
            "function": function_dir.name,
            "source_rel": source_rel.as_posix(),
            "source": source,
            "golden": csv_path.resolve(),
            "golden_rel": csv_path.relative_to(golden_root).as_posix(),
        })
    return result


def _match_golden(
    ir: Any, samples: list[dict[str, Any]], generated_path: Path,
) -> dict[str, Any]:
    source = Path(ir.file).resolve()
    matches = [
        item for item in samples
        if item["function"] == ir.name and item["source"] == source
    ]
    if len(matches) != 1:
        raise ValueError(
            f"could not uniquely pair {generated_path}: "
            f"function={ir.name!r}, source={source}, matches={len(matches)}"
        )
    return matches[0]


def build_corpus(
    generated_root: Path,
    golden_root: Path,
    source_root: Path,
    project_id: str,
) -> dict[str, Any]:
    generated_root = Path(generated_root).resolve()
    golden_root = Path(golden_root).resolve()
    source_root = Path(source_root).resolve()
    golden_samples = _golden_samples(golden_root, source_root)
    records: list[dict[str, Any]] = []
    pattern_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ir_path in sorted(
        generated_root.rglob("function-ir.json"),
        key=lambda path: path.as_posix().casefold(),
    ):
        document = json.loads(ir_path.read_text(encoding="utf-8"))
        ir = document_to_function_ir(document)
        golden = _match_golden(ir, golden_samples, ir_path)
        pattern = semantic_pattern(ir)
        record = {
            "sample_id": f"{golden['source_rel']}::{ir.name}",
            "project_id": project_id,
            "function": ir.name,
            "source_rel": golden["source_rel"],
            "golden_rel": golden["golden_rel"],
            "source": str(golden["source"]),
            "golden": str(golden["golden"]),
            "status": "EXTRACTED",
            "errors": [],
            "source_facts": {
                "function": ir.name,
                "file": str(ir.file),
                "line": ir.line,
                "ret_type": ir.ret_type,
                "params": len(ir.params),
                "branches": len(ir.branches),
                "atoms": sum(len(branch.atoms) for branch in ir.branches),
                "calls": len(ir.calls),
                "notes": list(ir.notes),
                "pattern": pattern,
            },
        }
        records.append(record)
        pattern_groups[pattern["pattern_id"]].append(record)

    patterns = [
        {
            "pattern_id": pattern_id,
            "occurrences": len(items),
            "examples": sorted(
                f"{item['source_rel']}::{item['function']}"
                for item in items
            )[:20],
            "template": items[0]["source_facts"]["pattern"]["shape"],
        }
        for pattern_id, items in sorted(pattern_groups.items())
    ]
    return {
        "schema_version": 1,
        "kind": "ut-agent-rule-corpus",
        "roots": {
            "evidence": str(golden_root),
            "source": str(source_root),
            "include": str(source_root),
            "function_ir": str(generated_root),
        },
        "config": {
            "project_id": project_id,
            "semantic_source": "Typed FunctionIR v3",
            "golden_values_are_evidence_only": True,
        },
        "counts": {
            "samples": len(records),
            "inferred": 0,
            "unsupported": 0,
            "patterns": len(patterns),
            "derived_rules": 0,
            "candidate_rules": 0,
        },
        "samples": records,
        "patterns": patterns,
        "candidate_pack": {
            "name": f"{project_id}-ir-evidence",
            "version": 1,
            "profile": {
                "base_profile": "PSD再構築",
                "profile_version": "PSD再構築-v1",
                "mcdc_enabled": True,
                "approved_exceptions": [],
            },
            "samples_are_evidence_only": True,
            "rules": [],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated-root", type=Path, required=True)
    parser.add_argument("--golden-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-pack", type=Path)
    args = parser.parse_args()
    result = build_corpus(
        args.generated_root, args.golden_root, args.source_root, args.project_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.candidate_pack:
        args.candidate_pack.parent.mkdir(parents=True, exist_ok=True)
        args.candidate_pack.write_text(
            json.dumps(result["candidate_pack"], ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
