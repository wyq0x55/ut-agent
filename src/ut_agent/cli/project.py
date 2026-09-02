"""CLI handlers for project, batch, index, and artifact commands."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .context import parse_defines


def run_batch(args) -> int:
    from ut_agent.batch import format_table, run_batch as execute_batch
    from ut_agent.toolchain import ClangExtractor

    results = execute_batch(
        Path(args.source),
        functions=args.functions.split(",") if args.functions else None,
        includes=args.include,
        defines=parse_defines(args.define),
        out_dir=Path(args.out),
        exec_limit=args.exec_limit,
        clang_extractor=(ClangExtractor(Path(args.clang_extractor))
                         if args.clang_extractor else None),
        include_config=(Path(args.include_config)
                        if args.include_config else None),
    )
    print(format_table(results))
    ok = sum(1 for result in results
             if result["status"] in {"VALIDATED", "OK"})
    print(f"\n[batch] VALIDATED {ok}/{len(results)}", file=sys.stderr)
    allowed = {"VALIDATED", "OK", "SKIP_EXEC"}
    return 0 if results and all(item["status"] in allowed for item in results) else 1


def run_index(args) -> int:
    from ut_agent.targets.winams.index import generate_project_from_index

    project_context = None
    if args.baseline_manifest:
        from ut_agent.project import resolve_project_context
        project_context = resolve_project_context(
            Path(args.baseline_manifest),
            config_root=Path(args.config_root) if args.config_root else None,
        )
    units = generate_project_from_index(
        Path(args.index_csv),
        Path(args.product_root),
        Path(args.out),
        reference_root=Path(args.reference_root) if args.reference_root else None,
        clang_extractor=(Path(args.clang_extractor)
                         if args.clang_extractor else None),
        rules_path=Path(args.rules) if args.rules else None,
        defines=parse_defines(args.define),
        call_max=args.call_max,
        extractor_timeout=args.extract_timeout,
        check_golden=args.check_golden,
        project_context=project_context,
    )
    statuses: dict[str, int] = {}
    for unit in units:
        statuses[unit.status] = statuses.get(unit.status, 0) + 1
    print(
        f"[index] units={len(units)} statuses={json.dumps(statuses, ensure_ascii=False, sort_keys=True)} "
        f"output={Path(args.out).resolve()}",
        file=sys.stderr,
    )
    if args.check_golden:
        report_path = Path(args.out).resolve() / "index-generation-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        comparison = report.get("comment_comparison", {})
        print(
            f"[index] #COMMENT equal={comparison.get('equal', 0)}/{comparison.get('total', 0)} "
            f"different={comparison.get('different', 0)} report={report_path}",
            file=sys.stderr,
        )
    return 0 if units else 1


def run_project(args) -> int:
    from ut_agent.targets.winams.project import (
        generate_project,
        run_winams,
    )
    from ut_agent.learning.compare import compare_testcsv

    reference_mode = args.reference_out is not None or args.reference_xlo is not None
    if reference_mode and not args.no_build:
        print("[project] reference 模式必须同时使用 --no-build", file=sys.stderr)
        return 2
    must_build = not reference_mode and (args.run or not args.no_build)
    project_context = None
    if args.baseline_manifest:
        from ut_agent.project import resolve_project_context
        project_context = resolve_project_context(
            Path(args.baseline_manifest),
            config_root=Path(args.config_root) if args.config_root else None,
        )
    generated = generate_project(
        Path(args.soft), Path(args.manifest), Path(args.out),
        build=must_build,
        compiler=args.compiler,
        converter=args.omf_converter,
        cpu=args.cpu,
        reference_out=Path(args.reference_out) if args.reference_out else None,
        reference_xlo=Path(args.reference_xlo) if args.reference_xlo else None,
        reference_mpu=args.reference_mpu if reference_mode else None,
        reference_define_var=(Path(args.reference_define_var)
                              if args.reference_define_var else None),
        clang_extractor=(Path(args.clang_extractor)
                         if args.clang_extractor else None),
        rules_path=Path(args.rules) if args.rules else None,
        project_context=project_context,
    )
    if args.check_golden:
        checks = compare_testcsv(generated)
        for name, ok in checks:
            has_golden = next(
                unit.expected is not None
                for unit in generated.units if unit.name == name
            )
            label = "OK" if ok and has_golden else "SKIP" if ok else "DIFF"
            print(f"[project/csv] {label} {name}", file=sys.stderr)
        if not all(ok for _, ok in checks):
            return 1
    else:
        print("[project/csv] AST-only generation; golden not used", file=sys.stderr)
    print(
        f"[project] {generated.root} units={len(generated.units)} "
        f"golden={sum(1 for unit in generated.units if unit.expected)}",
        file=sys.stderr,
    )
    print(
        "[project/define-var] generated="
        f"{sum(1 for unit in generated.units if unit.define_var and unit.define_var.is_file())}",
        file=sys.stderr,
    )
    for unit in generated.units:
        print(f"[project/rules] {unit.generation_status} {unit.name}",
              file=sys.stderr)
    if not args.run:
        return 0 if generated.units and all(
            unit.generation_status == "VALIDATED" for unit in generated.units
        ) else 1
    results = run_winams(generated, args.winams, timeout=args.timeout)
    for name, ok, detail in results:
        print(f"[project/run] {'OK' if ok else 'DIFF'} {name} {detail}",
              file=sys.stderr)
    return 0 if results and all(ok for _, ok, _ in results) else 1


def run_artifacts(args) -> int:
    from ut_agent.reporting import analyze_artifacts

    evidence = analyze_artifacts(
        Path(args.root),
        map_path=args.map_path,
        mot_path=args.mot_path,
        out_path=args.out_path,
        xlo_path=args.xlo_path,
    )
    rendered = json.dumps(evidence.to_dict(args.symbol), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    loaded = sum(item is not None for item in
                 (evidence.map, evidence.mot, evidence.out, evidence.xlo))
    print(f"[artifacts] loaded={loaded}/4 root={evidence.root}", file=sys.stderr)
    return 0
