"""CLI 入口：ut-agent parse <source.c> -f <函数名> ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ut_agent.parser import clang_parser


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ut-agent", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="解析源码 → FunctionIR JSON")
    p.add_argument("source", help="被测 .c 文件")
    p.add_argument("-f", "--function", help="被测函数名")
    p.add_argument("-I", "--include", action="append", default=[], help="include 目录（可多次）")
    p.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE",
                   help="宏定义（可多次）；结构性配置建议走配置头")
    p.add_argument("--include-config", help="强制包含的配置头路径")
    p.add_argument("--watch-macro", action="append", default=[],
                   help="需标记来源的函数宏（默认 VALIDATE_RV 等）")
    p.add_argument("-o", "--output", help="IR JSON 输出路径（缺省打印）")
    p.add_argument("--list", action="store_true", help="只列出文件内函数")

    g = sub.add_parser("gen", help="解析 + 生成 stub 源码与用例表 CSV")
    g.add_argument("source", help="被测 .c 文件")
    g.add_argument("-f", "--function", required=True, help="被测函数名")
    g.add_argument("-I", "--include", action="append", default=[])
    g.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE")
    g.add_argument("--include-config")
    g.add_argument("--call-max", type=int, default=16)
    g.add_argument("--cfg-display", default="", help="CSV 头部 CFG 行的展示文本")
    g.add_argument("--out", default=".", help="输出目录")
    b = sub.add_parser("batch", help="批量跑一个模块的全部函数（通用性验证）")
    b.add_argument("source", help="模块 .c 文件")
    b.add_argument("-f", "--functions", help="逗号分隔函数名列表（缺省=文件内全部）")
    b.add_argument("-I", "--include", action="append", default=[])
    b.add_argument("-D", "--define", action="append", default=[], metavar="NAME=VALUE")
    b.add_argument("--include-config")
    b.add_argument("--out", default=".build/batch")
    b.add_argument("--exec-limit", type=int, default=20000)
    a = ap.parse_args(argv)

    if a.cmd == "batch":
        from ut_agent.batch import format_table, run_batch

        defines = {}
        for d in a.define:
            k, _, v = d.partition("=")
            defines[k] = v
        funcs = a.functions.split(",") if a.functions else None
        results = run_batch(Path(a.source), functions=funcs, includes=a.include,
                            defines=defines, out_dir=Path(a.out),
                            exec_limit=a.exec_limit)
        print(format_table(results))
        ok = sum(1 for r in results if r["status"] == "OK")
        print(f"\n[batch] OK {ok}/{len(results)}", file=sys.stderr)
        allowed = {"OK", "SKIP_EXEC"}
        return 0 if results and all(r["status"] in allowed for r in results) else 1

    if a.cmd == "gen":
        from ut_agent.cases import boundary
        from ut_agent.stub import generate as stub_gen
        from ut_agent.winams import csv_render

        defines = {}
        for d in a.define:
            k, _, v = d.partition("=")
            defines[k] = v
        tu = clang_parser.parse_tu(
            Path(a.source), a.include, defines,
            Path(a.include_config) if a.include_config else None, strict=False)
        ir = clang_parser.extract_function(tu, Path(a.source), a.function, defines)

        out_dir = Path(a.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        stub_path = out_dir / f"{a.function}_stubs.c"
        csv_path = out_dir / f"{a.function}_testdata.csv"
        stub_path.write_text(stub_gen.render_stub_c(ir, a.call_max), encoding="utf-8")
        _, rows = boundary.enumerate_rows(ir)
        csv_path.write_text(csv_render.render_csv(ir, a.cfg_display or str(defines)),
                            encoding="utf-8")
        n_atoms = sum(len(b.atoms) for b in ir.branches)
        print(f"[gen] {stub_path} / {csv_path} | branches={len(ir.branches)} "
              f"atoms={n_atoms} rows={len(rows)}", file=sys.stderr)
        return 0

    defines: dict[str, str] = {}
    for d in a.define:
        k, _, v = d.partition("=")
        defines[k] = v

    tu = clang_parser.parse_tu(
        Path(a.source), a.include, defines,
        Path(a.include_config) if a.include_config else None,
    )
    if a.list:
        for f in clang_parser.list_functions(tu, Path(a.source)):
            print(f)
        return 0

    ir = clang_parser.extract_function(
        tu, Path(a.source), a.function, defines,
        a.watch_macro or clang_parser.WATCH_MACROS_DEFAULT,
    )
    out = json.dumps(ir.to_dict(), ensure_ascii=False, indent=2)
    if a.output:
        Path(a.output).write_text(out, encoding="utf-8")
    else:
        print(out)
    n_atoms = sum(len(b.atoms) for b in ir.branches)
    print(
        f"[summary] {ir.name}: params={len(ir.params)} "
        f"calls={[c.callee for c in ir.calls]} branches={len(ir.branches)} atoms={n_atoms}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
