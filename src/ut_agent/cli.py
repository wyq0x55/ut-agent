"""CLI 入口：ut-agent parse <source.c> -f <函数名> ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _default_clang_extractor() -> str:
    """Use the repository-built C++ LibTooling extractor.

    A missing executable is a build/configuration error.  The CLI never
    selects a Python parser as a fallback.
    """
    from ut_agent.parser.clang_extractor import default_clang_extractor

    return str(default_clang_extractor())


def _source_context_root(source: Path) -> Path:
    """Use the nearest ``src`` ancestor for deterministic source discovery."""
    source = source.resolve()
    for parent in (source.parent, *source.parents):
        if parent.name.lower() == "src":
            return parent
    return source.parent


def _compile_sources(source: Path, context_sources: list[str], *, discover: bool) -> tuple[Path, ...]:
    """Build a C++ extractor context without inspecting source text."""
    from ut_agent.parser import discover_compile_sources

    source = Path(source).resolve()
    explicit = tuple(Path(item).resolve() for item in context_sources)
    if explicit:
        return (source, *(item for item in explicit if item != source))
    if discover:
        return discover_compile_sources(_source_context_root(source), source)
    return (source,)



def _configure_console_encoding() -> None:
    """Windows 的 CP932 终端无法显示简体中文帮助，统一改为 UTF-8。"""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    _configure_console_encoding()
    ap = argparse.ArgumentParser(prog="ut-agent", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="解析源码 → FunctionIR JSON")
    p.add_argument("source", help="被测 .c 文件")
    p.add_argument("-f", "--function", help="被测函数名")
    p.add_argument("-I", "--include", action="append", default=[], help="include 目录（可多次）")
    p.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE",
                   help="宏定义（可多次）；结构性配置建议走配置头")
    p.add_argument("--include-config", help="强制包含的配置头路径")
    p.add_argument("--context-source", action="append", default=[],
                   help="全局/静态初始化所在的 C 源文件（可多次指定）")
    p.add_argument("--watch-macro", action="append", default=[],
                   help="需标记来源的函数宏（默认 VALIDATE_RV 等）")
    p.add_argument("--clang-extractor", default=None,
                   help="使用指定的 standalone Clang extractor")
    p.add_argument("-o", "--output", help="IR JSON 输出路径（缺省打印）")
    p.add_argument("--list", action="store_true", help="只列出文件内函数")

    g = sub.add_parser("gen", help="解析 + 生成 stub 源码与用例表 CSV")
    g.add_argument("source", help="被测 .c 文件")
    g.add_argument("-f", "--function", required=True, help="被测函数名")
    g.add_argument("-I", "--include", action="append", default=[])
    g.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE")
    g.add_argument("--include-config")
    g.add_argument("--context-source", action="append", default=[],
                   help="全局/静态初始化所在的 C 源文件（可多次指定）")
    g.add_argument("--call-max", type=int, default=5,
                   help="WinAMS CALL_MAX（参考 AMSTB_SrcFile.c=5；按函数调用次数可调大）")
    g.add_argument("--cfg-display", default="", help="CSV 头部 CFG 行的展示文本")
    g.add_argument("--reference-csv", help="已废弃：TestCsv 只可用于生成后对照，不参与生成")
    g.add_argument("--rules", help="已审批的 JSON 规则包")
    g.add_argument("--clang-extractor", default=None,
                   help="使用指定的 standalone Clang extractor")
    g.add_argument("--intent-manifest", help="测试意图 manifest 路径；缺省写入输出目录")
    g.add_argument("--winams-source-label", default="", help="WinAMS mod 行的被测对象标识")
    g.add_argument("--winams-title", default="", help="WinAMS mod 行的测试标题")
    g.add_argument("--out", default=".", help="输出目录")
    b = sub.add_parser("batch", help="批量跑一个模块的全部函数（通用性验证）")
    b.add_argument("source", help="模块 .c 文件")
    b.add_argument("-f", "--functions", help="逗号分隔函数名列表（缺省=文件内全部）")
    b.add_argument("-I", "--include", action="append", default=[])
    b.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE")
    b.add_argument("--include-config")
    b.add_argument("--clang-extractor", default=None,
                   help="使用指定的 standalone Clang extractor")
    b.add_argument("--out", default=".build/batch")
    b.add_argument("--exec-limit", type=int, default=20000)
    psd = sub.add_parser(
        "psd-project",
        help="用项目索引驱动一次 C++ Clang 提取并生成全部 WinAMS TestCsv",
    )
    psd.add_argument("index_csv", help="五列项目函数索引 CSV")
    psd.add_argument("--product-root", required=True, help="Soft Product 根目录")
    psd.add_argument("--out", required=True, help="生成工程输出目录")
    psd.add_argument("--reference-root", help="原项目根目录，仅用于可选注释对比")
    psd.add_argument("--check-golden", action="store_true",
                     help="只读对比原 WinAMS CSV 的 #COMMENT 行")
    psd.add_argument("--call-max", type=int, default=5)
    psd.add_argument("--extract-timeout", type=float, default=600.0,
                     help="一次 C++ 提取的超时秒数")
    psd.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE")
    psd.add_argument("--rules", help="已审批的 JSON 规则包")
    psd.add_argument("--clang-extractor", default=None,
                     help="使用指定的 standalone Clang extractor")
    arm = sub.add_parser("arm-build", help="使用 Arm GNU Toolchain 生成带 DWARF 的 ARM ELF")
    arm.add_argument("source", nargs="+", help="一个或多个 C 源文件")
    arm.add_argument("-I", "--include", action="append", default=[])
    arm.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE")
    arm.add_argument("-o", "--output", required=True, help="输出 ELF 路径")
    arm.add_argument("--compiler", help="arm-none-eabi-gcc 路径；缺省自动查找")
    arm.add_argument("--cpu", default="cortex-m4")
    arm.add_argument("--entry", help="ELF 入口函数，例如 p_vog_dma_init")
    arm.add_argument("--strict-link", action="store_true",
                     help="不允许硬件符号未解析（普通 WinAMS 函数隔离通常不需要）")
    arm.add_argument("--omf-output", help="可选：调用 WinAMS armgccomf 生成 .xlo")
    arm.add_argument("--omf-converter", help="armgccomf.EXE 路径")
    arm.add_argument("--dwarf-version", type=int, choices=(4, 5), default=5)
    project = sub.add_parser(
        "project", help="只用 Soft + manifest 生成并可运行完整 WinAMS 工程"
    )
    project.add_argument("soft", help="用户交付的 Soft 根目录")
    project.add_argument("--manifest", required=True, help="仓库内项目 manifest")
    project.add_argument("--out", required=True, help="空工程输出目录")
    project.add_argument("--no-build", action="store_true",
                         help="只生成 src/stub/TestCsv/amsy，不编译 ELF/xlo")
    project.add_argument("--run", action="store_true",
                         help="调用 SSTManager.exe，并比较 golden Output")
    project.add_argument("--check-golden", action="store_true",
                         help="生成后只读对比 golden TestCsv；不参与生成")
    project.add_argument("--compiler", help="arm-none-eabi-gcc 路径")
    project.add_argument("--omf-converter", help="armgccomf.EXE 路径")
    project.add_argument("--cpu", help="覆盖 manifest CPU，例如 rh850 或 cortex-m3")
    project.add_argument("--reference-out",
                         help="只读引用原项目 .out，不参与新工程编译")
    project.add_argument("--reference-xlo",
                         help="只读引用原项目 .out.xlo，不参与新工程编译")
    project.add_argument("--reference-define-var",
                         help="可选：指定原工程 DefineVar.dat；缺省按 Soft/work/winAMS 自动定位")
    project.add_argument("--reference-mpu", default="RH850(GHS)",
                         help="reference 产物对应的 WinAMS MPU 名称；RH850(GHS) 会归一化为 RH850")
    project.add_argument("--winams", default="/mnt/c/WinAMS/BIN/SSTManager.exe",
                         help="SSTManager.exe 路径")
    project.add_argument("--timeout", type=float, default=120.0,
                         help="每个 WinAMS 用例超时秒数")
    project.add_argument("--clang-extractor", default=None,
                         help="使用指定的 standalone Clang extractor")
    project.add_argument("--rules", help="已审批的 JSON 规则包")
    rules = sub.add_parser("rules", help="规则包归纳与审查辅助")
    rules_sub = rules.add_subparsers(dest="rules_cmd", required=True)
    infer = rules_sub.add_parser("infer", help="从人工 TestCsv 归纳候选规则")
    infer.add_argument("source", help="被测 .c 文件")
    infer.add_argument("-f", "--function", required=True, help="被测函数名")
    infer.add_argument("--golden", required=True, help="人工 WinAMS TestCsv")
    infer.add_argument("-I", "--include", action="append", default=[])
    infer.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE")
    infer.add_argument("--include-config")
    infer.add_argument("--clang-extractor", default=None,
                       help="使用指定的 standalone Clang extractor")
    infer.add_argument("-o", "--output", required=True, help="候选规则 JSON 输出路径")
    infer.add_argument("--merge", action="store_true",
                       help="输出已存在时合并候选规则，供多函数规则包归纳")
    collect = rules_sub.add_parser(
        "collect", help="批量采集完整 WinAMS TestCsv 语料并生成候选规则包"
    )
    collect.add_argument("winams_root", help="包含各源文件目录的 winAMS/src 根目录")
    collect.add_argument("--base-profile", default="PSD再構築",
                         help="基础测试 Profile 名称")
    collect.add_argument("--profile-version", default="PSD再構築-v1",
                         help="Profile 版本标识")
    collect.add_argument("--mcdc-enabled", action=argparse.BooleanOptionalAction,
                         default=True, help="是否启用 MC/DC 维度（默认启用）")
    collect.add_argument("--exception", action="append", default=[], dest="exceptions",
                         help="已审批的版本化例外标识，可重复指定")
    collect.add_argument("--source-root", help="对应 Soft/src 根目录（可选自动推导）")
    collect.add_argument("--include-root", help="递归发现 include 目录的根目录")
    collect.add_argument("-I", "--include", action="append", default=[],
                         help="显式 include 目录；指定后不递归扫描 include-root")
    collect.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE")
    collect.add_argument("--include-config", help="强制包含的配置头路径")
    collect.add_argument("--max-samples", type=int, help="限制采集样本数（调试用）")
    collect.add_argument("-o", "--output", required=True, help="语料报告 JSON 输出路径")
    collect.add_argument("--candidate-pack", help="候选规则包 JSON 输出路径")
    compress = rules_sub.add_parser(
        "compress", help="将语料模式压缩为跨函数/跨项目语义族"
    )
    compress.add_argument("corpus", nargs="+",
                          help="一个或多个 rules collect 语料报告 JSON")
    compress.add_argument("-o", "--output", required=True,
                          help="压缩报告 JSON 输出路径")
    compress.add_argument("--project-id", action="append", dest="project_ids",
                          help="语料所属项目编号（多语料时按顺序重复指定）")
    compress.add_argument("--min-functions", type=int, default=2,
                          help="晋升为跨函数规则所需的最少函数数")
    compress.add_argument("--min-projects", type=int, default=2,
                          help="晋升为跨项目规则所需的最少项目数")
    check = rules_sub.add_parser("check", help="检查规则包结构和审批状态")
    check.add_argument("pack", help="规则包 JSON")
    check.add_argument("--require-approved", action="store_true",
                       help="要求全部项目规则已审批")
    approve = rules_sub.add_parser("approve", help="将候选规则复制为已审批规则包")
    approve.add_argument("pack", help="候选规则包 JSON")
    approve.add_argument("-o", "--output", required=True, help="已审批规则包输出路径")
    approve.add_argument("--authority", required=True, help="审批人或审批记录标识")
    approve.add_argument("--reason", required=True, help="审批依据")
    approve.add_argument("--id", action="append", dest="rule_ids",
                         help="只批准指定规则 ID，可重复指定")
    artifacts = sub.add_parser(
        "artifacts", help="只读分析 Soft.map/Soft.mot/Soft.out/Soft.out.xlo 证据"
    )
    artifacts.add_argument("root", help="包含 Soft 构建产物的目录")
    artifacts.add_argument("--map", dest="map_path", help="覆盖默认 Soft.map 路径")
    artifacts.add_argument("--mot", dest="mot_path", help="覆盖默认 Soft.mot 路径")
    artifacts.add_argument("--out", dest="out_path", help="覆盖默认 Soft.out 路径")
    artifacts.add_argument("--xlo", dest="xlo_path", help="覆盖默认 Soft.out.xlo 路径")
    artifacts.add_argument("--symbol", action="append", default=[],
                           help="交叉核对函数符号，可重复指定")
    artifacts.add_argument("-o", "--output", help="输出 JSON 文件；缺省打印到标准输出")
    a = ap.parse_args(argv)

    if a.cmd == "batch":
        from ut_agent.batch import format_table, run_batch
        from ut_agent.parser import ClangExtractor

        defines = {}
        for d in a.define:
            k, _, v = d.partition("=")
            defines[k] = v
        funcs = a.functions.split(",") if a.functions else None
        results = run_batch(Path(a.source), functions=funcs, includes=a.include,
                            defines=defines, out_dir=Path(a.out),
                            exec_limit=a.exec_limit,
                            clang_extractor=(
                                ClangExtractor(Path(a.clang_extractor))
                                if a.clang_extractor else None
                            ),
                            include_config=(
                                Path(a.include_config) if a.include_config else None
                            ))
        print(format_table(results))
        ok = sum(1 for r in results if r["status"] in {"VALIDATED", "OK"})
        print(f"\n[batch] VALIDATED {ok}/{len(results)}", file=sys.stderr)
        # 旧状态只为兼容外部调用者；run_batch 本身只产生新状态。
        allowed = {"VALIDATED", "OK", "SKIP_EXEC"}
        return 0 if results and all(r["status"] in allowed for r in results) else 1

    if a.cmd == "psd-project":
        from ut_agent.winams.psd_project import generate_psd_project

        defines = {}
        for definition in a.define:
            key, _, value = definition.partition("=")
            defines[key] = value
        units = generate_psd_project(
            Path(a.index_csv),
            Path(a.product_root),
            Path(a.out),
            reference_root=Path(a.reference_root) if a.reference_root else None,
            clang_extractor=(Path(a.clang_extractor)
                             if a.clang_extractor else None),
            rules_path=Path(a.rules) if a.rules else None,
            defines=defines,
            call_max=a.call_max,
            extractor_timeout=a.extract_timeout,
            check_golden=a.check_golden,
        )
        statuses = {}
        for unit in units:
            statuses[unit.status] = statuses.get(unit.status, 0) + 1
        print(
            f"[psd-project] units={len(units)} statuses={json.dumps(statuses, ensure_ascii=False, sort_keys=True)} "
            f"output={Path(a.out).resolve()}",
            file=sys.stderr,
        )
        if a.check_golden:
            report_path = Path(a.out).resolve() / "psd-generation-report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            comparison = report.get("comment_comparison", {})
            print(
                f"[psd-project] #COMMENT equal={comparison.get('equal', 0)}/{comparison.get('total', 0)} "
                f"different={comparison.get('different', 0)} report={report_path}",
                file=sys.stderr,
            )
        return 0 if len(units) > 0 else 1

    if a.cmd == "arm-build":
        from ut_agent.host.arm_gcc import (
            ArmGccConfig, build_elf, convert_to_winams_omf, find_arm_gcc,
        )

        defines = {}
        for d in a.define:
            k, _, v = d.partition("=")
            defines[k] = v
        compiler = find_arm_gcc(a.compiler)
        config = ArmGccConfig(compiler=compiler, cpu=a.cpu)
        output, objects = build_elf(
            [Path(item) for item in a.source], Path(a.output), config,
            include_dirs=a.include, defines=defines, entry=a.entry,
            allow_unresolved=not a.strict_link,
        )
        print(f"[arm-build] {output} objects={len(objects)}", file=sys.stderr)
        if a.omf_output:
            omf = convert_to_winams_omf(
                output, Path(a.omf_output), a.omf_converter, a.dwarf_version
            )
            print(f"[arm-build] {omf} (WinAMS OMF)", file=sys.stderr)
        return 0

    if a.cmd == "project":
        from ut_agent.winams.project import (
            compare_testcsv,
            generate_project,
            run_winams,
        )

        reference_mode = a.reference_out is not None or a.reference_xlo is not None
        if reference_mode and not a.no_build:
            print("[project] reference 模式必须同时使用 --no-build", file=sys.stderr)
            return 2
        must_build = not reference_mode and (a.run or not a.no_build)
        generated = generate_project(
            Path(a.soft), Path(a.manifest), Path(a.out),
            build=must_build, compiler=a.compiler,
            converter=a.omf_converter,
            cpu=a.cpu,
            reference_out=Path(a.reference_out) if a.reference_out else None,
            reference_xlo=Path(a.reference_xlo) if a.reference_xlo else None,
            reference_mpu=a.reference_mpu if reference_mode else None,
            reference_define_var=(
                Path(a.reference_define_var) if a.reference_define_var else None
            ),
            clang_extractor=Path(a.clang_extractor) if a.clang_extractor else None,
            rules_path=Path(a.rules) if a.rules else None,
        )
        if a.check_golden:
            csv_checks = compare_testcsv(generated)
            for name, ok in csv_checks:
                has_golden = next(
                    u.expected is not None for u in generated.units if u.name == name
                )
                label = "OK" if ok and has_golden else "SKIP" if ok else "DIFF"
                print(f"[project/csv] {label} {name}", file=sys.stderr)
            if not all(ok for _, ok in csv_checks):
                return 1
        else:
            print("[project/csv] AST-only generation; golden not used", file=sys.stderr)
        print(
            f"[project] {generated.root} units={len(generated.units)} "
            f"golden={sum(1 for u in generated.units if u.expected)}",
            file=sys.stderr,
        )
        print(
            f"[project/define-var] generated="
            f"{sum(1 for u in generated.units if u.define_var and u.define_var.is_file())}",
            file=sys.stderr,
        )
        statuses = {unit.name: unit.generation_status for unit in generated.units}
        for name, status in statuses.items():
            print(f"[project/rules] {status} {name}", file=sys.stderr)
        if not a.run:
            return 0 if statuses and all(value == "VALIDATED" for value in statuses.values()) else 1
        results = run_winams(generated, a.winams, timeout=a.timeout)
        for name, ok, detail in results:
            print(f"[project/run] {'OK' if ok else 'DIFF'} {name} {detail}",
                  file=sys.stderr)
        return 0 if results and all(ok for _, ok, _ in results) else 1

    if a.cmd == "artifacts":
        from ut_agent.artifacts import analyze_artifacts

        evidence = analyze_artifacts(
            Path(a.root),
            map_path=a.map_path,
            mot_path=a.mot_path,
            out_path=a.out_path,
            xlo_path=a.xlo_path,
        )
        text = json.dumps(
            evidence.to_dict(a.symbol), ensure_ascii=False, indent=2,
        )
        if a.output:
            Path(a.output).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        loaded = sum(item is not None for item in (
            evidence.map, evidence.mot, evidence.out, evidence.xlo,
        ))
        print(f"[artifacts] loaded={loaded}/4 root={evidence.root}", file=sys.stderr)
        return 0

    if a.cmd == "rules":
        from ut_agent.rules import approve_rule_pack, review_rule_pack
        from ut_agent.winams.rule_infer import infer_rule_pack

        if a.rules_cmd == "check":
            try:
                report = review_rule_pack(Path(a.pack))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"[rules/check] INVALID {exc}", file=sys.stderr)
                return 1
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if a.require_approved and report["counts"]["candidate"]:
                print("[rules/check] 仍有未审批候选规则", file=sys.stderr)
                return 1
            return 0

        if a.rules_cmd == "approve":
            try:
                output = approve_rule_pack(
                    Path(a.pack), Path(a.output), authority=a.authority,
                    reason=a.reason, rule_ids=set(a.rule_ids) if a.rule_ids else None,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"[rules/approve] INVALID {exc}", file=sys.stderr)
                return 1
            print(f"[rules/approve] output={output}", file=sys.stderr)
            return 0

        if a.rules_cmd == "collect":
            from ut_agent.parser.rule_corpus import collect_rule_corpus

            defines = {}
            for item in a.define:
                key, _, value = item.partition("=")
                defines[key] = value
            try:
                report = collect_rule_corpus(
                    Path(a.winams_root),
                    base_profile=a.base_profile,
                    profile_version=a.profile_version,
                    mcdc_enabled=a.mcdc_enabled,
                    approved_exceptions=a.exceptions,
                    source_root=Path(a.source_root) if a.source_root else None,
                    include_root=Path(a.include_root) if a.include_root else None,
                    include_dirs=[Path(item) for item in a.include] or None,
                    defines=defines,
                    force_include=Path(a.include_config) if a.include_config else None,
                    output=Path(a.output),
                    candidate_pack=(Path(a.candidate_pack)
                                    if a.candidate_pack else None),
                    max_samples=a.max_samples,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"[rules/collect] INVALID {exc}", file=sys.stderr)
                return 1
            print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
            print(
                f"[rules/collect] samples={report['counts']['samples']} "
                f"inferred={report['counts']['inferred']} "
                f"candidate_rules={report['counts']['candidate_rules']} "
                f"output={Path(a.output)}",
                file=sys.stderr,
            )
            return 0 if report["counts"]["unsupported"] == 0 else 1

        if a.rules_cmd == "compress":
            from ut_agent.rules.compress import compress_corpus_file, compress_corpora

            try:
                if len(a.corpus) == 1:
                    report = compress_corpus_file(
                        Path(a.corpus[0]), Path(a.output),
                        project_id=(a.project_ids[0] if a.project_ids else None),
                        min_functions=a.min_functions,
                        min_projects=a.min_projects,
                    )
                else:
                    corpora = [
                        json.loads(Path(item).read_text(encoding="utf-8"))
                        for item in a.corpus
                    ]
                    report = compress_corpora(corpora,
                                               project_ids=a.project_ids,
                                               min_functions=a.min_functions,
                                               min_projects=a.min_projects)
                    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
                    Path(a.output).write_text(
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
                f"output={Path(a.output)}",
                file=sys.stderr,
            )
            return 0

        defines = {}
        for definition in a.define:
            key, _, value = definition.partition("=")
            defines[key] = value
        source = Path(a.source)
        from ut_agent.parser import ClangExtractor, make_compile_context
        from ut_agent.parser import default_clang_extractor
        extractor = ClangExtractor(
            Path(a.clang_extractor) if a.clang_extractor
            else default_clang_extractor()
        )
        context = make_compile_context(
            _compile_sources(source, [], discover=True),
            a.include,
            defines,
            [Path(a.include_config)] if a.include_config else (),
        )
        ir = extractor.extract_from_source(
            context, a.function, source, cwd=source.parent
        )
        inferred = infer_rule_pack(ir, Path(a.golden))
        output = Path(a.output)
        if a.merge and output.is_file():
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
        print(
            f"[rules/infer] candidate={len(inferred['rules'])} output={output}",
            file=sys.stderr,
        )
        return 0

    if a.cmd == "gen":
        from ut_agent.parser import ClangExtractor, make_compile_context
        from ut_agent.rules import generate_intents, load_rule_pack
        from ut_agent.stub import generate as stub_gen
        from ut_agent.winams import csv_render

        if a.reference_csv:
            raise ValueError(
                "--reference-csv 不能用于生成；请先生成 AST/Clang CSV，再单独执行对照"
            )

        defines = {}
        for d in a.define:
            k, _, v = d.partition("=")
            defines[k] = v
        source = Path(a.source)
        context = make_compile_context(
            _compile_sources(source, a.context_source, discover=True),
            a.include,
            defines,
            [Path(a.include_config)] if a.include_config else (),
        )
        from ut_agent.parser import default_clang_extractor
        extractor = ClangExtractor(
            Path(a.clang_extractor) if a.clang_extractor
            else default_clang_extractor()
        )
        ir = extractor.extract_from_source(
            context, a.function, source, cwd=source.parent
        )

        out_dir = Path(a.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        stub_path = out_dir / f"{a.function}_stubs.c"
        csv_path = out_dir / f"{a.function}_testdata.csv"
        stub_path.write_text(stub_gen.render_stub_c(ir, a.call_max), encoding="utf-8")
        pack = load_rule_pack(Path(a.rules) if a.rules else None)
        generation = generate_intents(ir, pack)
        csv_text = csv_render.render_intents_csv(
            ir, generation,
            source_label=a.winams_source_label or None,
            title=a.winams_title or None,
        )
        csv_path.write_bytes(csv_text.encode("cp932"))
        manifest_path = (Path(a.intent_manifest) if a.intent_manifest
                         else out_dir / f"{a.function}_test-intents.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(generation.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        n_atoms = sum(len(b.atoms) for b in ir.branches)
        print(f"[gen] {stub_path} / {csv_path} | branches={len(ir.branches)} "
              f"atoms={n_atoms} intents={len(generation.validated_intents)} "
              f"status={generation.status}", file=sys.stderr)
        return 0 if generation.status == "VALIDATED" else 1

    defines: dict[str, str] = {}
    for d in a.define:
        k, _, v = d.partition("=")
        defines[k] = v

    from ut_agent.parser import ClangExtractor, make_compile_context
    from ut_agent.parser.ir_json import serialize_document

    source = Path(a.source)
    from ut_agent.parser import default_clang_extractor
    client = ClangExtractor(
        Path(a.clang_extractor) if a.clang_extractor
        else default_clang_extractor()
    )
    context = make_compile_context(
        _compile_sources(source, a.context_source, discover=not a.list),
        a.include,
        defines,
        [Path(a.include_config)] if a.include_config else (),
    )
    if a.list:
        for function in client.extract_all(context, cwd=source.parent):
            if Path(function.file).resolve() == source.resolve():
                print(f"{function.name}:{function.line}")
        return 0
    if not a.function:
        print("parse requires --function unless --list is used", file=sys.stderr)
        return 2
    ir = client.extract_from_source(
        context, a.function, source, cwd=source.parent
    )
    out = serialize_document(ir.to_dict())
    if a.output:
        Path(a.output).write_text(out, encoding="utf-8")
    else:
        print(out, end="")
    n_atoms = sum(len(b.atoms) for b in ir.branches)
    print(
        f"[summary] {ir.name}: params={len(ir.params)} "
        f"calls={[c.callee for c in ir.calls]} branches={len(ir.branches)} atoms={n_atoms}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
