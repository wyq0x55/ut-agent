"""CLI handlers for deterministic rule-pack review and learning workflows."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .context import compile_sources, parse_defines


def _extract(args):
    from ut_agent.toolchain import ClangExtractor, default_clang_extractor, make_compile_context

    source = Path(args.source)
    context = make_compile_context(
        compile_sources(source, [], discover=True),
        args.include,
        parse_defines(args.define),
        [Path(args.include_config)] if args.include_config else (),
    )
    extractor = ClangExtractor(
        Path(args.clang_extractor) if args.clang_extractor
        else default_clang_extractor()
    )
    return extractor.extract_from_source(context, args.function, source, cwd=source.parent)


def run(args) -> int:
    from ut_agent.generation import approve_rule_pack, review_rule_pack

    if args.rules_cmd == "check":
        try:
            report = review_rule_pack(Path(args.pack))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[rules/check] INVALID {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.require_approved and report["counts"]["candidate"]:
            print("[rules/check] 仍有未审批候选规则", file=sys.stderr)
            return 1
        return 0

    if args.rules_cmd == "approve":
        try:
            output = approve_rule_pack(
                Path(args.pack), Path(args.output), authority=args.authority,
                reason=args.reason,
                rule_ids=set(args.rule_ids) if args.rule_ids else None,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[rules/approve] INVALID {exc}", file=sys.stderr)
            return 1
        print(f"[rules/approve] output={output}", file=sys.stderr)
        return 0

    if args.rules_cmd == "collect":
        from ut_agent.learning.corpus import collect_rule_corpus

        try:
            report = collect_rule_corpus(
                Path(args.winams_root),
                base_profile=args.base_profile,
                profile_version=args.profile_version,
                mcdc_enabled=args.mcdc_enabled,
                approved_exceptions=args.exceptions,
                source_root=Path(args.source_root) if args.source_root else None,
                include_root=Path(args.include_root) if args.include_root else None,
                include_dirs=[Path(item) for item in args.include] or None,
                defines=parse_defines(args.define),
                force_include=(Path(args.include_config)
                               if args.include_config else None),
                output=Path(args.output),
                candidate_pack=(Path(args.candidate_pack)
                                if args.candidate_pack else None),
                max_samples=args.max_samples,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[rules/collect] INVALID {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
        print(
            f"[rules/collect] samples={report['counts']['samples']} "
            f"inferred={report['counts']['inferred']} "
            f"candidate_rules={report['counts']['candidate_rules']} "
            f"output={Path(args.output)}",
            file=sys.stderr,
        )
        return 0 if report["counts"]["unsupported"] == 0 else 1

    if args.rules_cmd == "compress":
        from ut_agent.learning.compress import compress_corpus_file, compress_corpora

        try:
            if len(args.corpus) == 1:
                report = compress_corpus_file(
                    Path(args.corpus[0]), Path(args.output),
                    project_id=(args.project_ids[0] if args.project_ids else None),
                    min_functions=args.min_functions,
                    min_projects=args.min_projects,
                )
            else:
                corpora = [
                    json.loads(Path(item).read_text(encoding="utf-8"))
                    for item in args.corpus
                ]
                report = compress_corpora(
                    corpora,
                    project_ids=args.project_ids,
                    min_functions=args.min_functions,
                    min_projects=args.min_projects,
                )
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[rules/compress] INVALID {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
        print(
            f"[rules/compress] families={report['counts']['families']} "
            f"cross_function={report['counts']['cross_function']} "
            f"cross_project={report['counts']['cross_project']} "
            f"output={Path(args.output)}",
            file=sys.stderr,
        )
        return 0

    from ut_agent.learning.rule_infer import infer_rule_pack

    ir = _extract(args)
    inferred = infer_rule_pack(ir, Path(args.golden))
    output = Path(args.output)
    if args.merge and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        old_rules = list(existing.get("rules", []))
        new_ids = {rule["id"] for rule in inferred["rules"]}
        old_rules = [rule for rule in old_rules if rule.get("id") not in new_ids]
        inferred = {
            "name": str(existing.get("name", inferred["name"])),
            "version": max(int(existing.get("version", 1)), inferred["version"]),
            "rules": old_rules + inferred["rules"],
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(inferred, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[rules/infer] candidate={len(inferred['rules'])} output={output}",
          file=sys.stderr)
    return 0
