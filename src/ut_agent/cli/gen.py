"""CLI handler for AST/FunctionIR to semantic-intent WinAMS projection."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .context import compile_sources, extractor, parse_defines


def run(args) -> int:
    from ut_agent.generation import generate_intents, generate_suite, load_rule_pack
    from ut_agent.targets.winams import csv as csv_adapter
    from ut_agent.targets.winams import stub
    from ut_agent.toolchain import make_compile_context

    if args.reference_csv:
        raise ValueError(
            "--reference-csv 不能用于生成；请先生成 AST/Clang CSV，再单独执行对照"
        )
    source = Path(args.source)
    context = make_compile_context(
        compile_sources(source, args.context_source, discover=True),
        args.include, parse_defines(args.define),
        [Path(args.include_config)] if args.include_config else (),
    )
    ir = extractor(args.clang_extractor).extract_from_source(
        context, args.function, source, cwd=source.parent
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stub_path = out_dir / f"{args.function}_stubs.c"
    csv_path = out_dir / f"{args.function}_testdata.csv"
    stub_path.write_text(stub.render_stub_c(ir, args.call_max), encoding="utf-8")
    if args.project_manifest:
        from ut_agent.project import resolve_project_context
        context = resolve_project_context(
            Path(args.project_manifest),
            config_root=Path(args.config_root) if args.config_root else None,
        )
        suite = generate_suite(ir, context)
        generation_document = suite.to_dict()
        if suite.status == "VALIDATED":
            csv_text = csv_adapter.render_suite_csv(
                ir, suite, source_label=args.winams_source_label or None,
                title=args.winams_title or None,
            )
            csv_path.write_bytes(csv_text.encode("cp932"))
        else:
            generation_document = {**generation_document, "csv_written": False}
        generation_status = suite.status
        intent_count = len(suite.intents)
    else:
        generation = generate_intents(ir, load_rule_pack(Path(args.rules) if args.rules else None))
        csv_text = csv_adapter.render_intents_csv(
            ir, generation, source_label=args.winams_source_label or None,
            title=args.winams_title or None,
        )
        csv_path.write_bytes(csv_text.encode("cp932"))
        generation_document = generation.to_dict()
        generation_status = generation.status
        intent_count = len(generation.validated_intents)
    manifest_path = (Path(args.intent_manifest) if args.intent_manifest
                     else out_dir / f"{args.function}_test-intents.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(generation_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    atoms = sum(len(branch.atoms) for branch in ir.branches)
    print(
        f"[gen] {stub_path} / {csv_path} | branches={len(ir.branches)} "
        f"atoms={atoms} intents={intent_count} "
        f"status={generation_status}", file=sys.stderr,
    )
    return 0 if generation_status == "VALIDATED" else 1
