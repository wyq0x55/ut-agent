"""批量通用性验证：同一套流水线跑模块内全部函数，暴露框架缺口。

状态分类：VALIDATED / NEEDS_REVIEW / UNSUPPORTED / EXECUTION_FAILED。
全部确定性；结果表用于评估框架通用率与缺口清单。
"""
from __future__ import annotations

from pathlib import Path

from ut_agent.cases import boundary
from ut_agent.host import driver as driver_gen
from ut_agent.host import extract, run
from ut_agent.parser import (
    ClangExtractor,
    default_clang_extractor,
    discover_compile_sources,
    make_compile_context,
)
from ut_agent.winams.csv_render import build_columns

EXEC_LIMIT = 20000   # 用例数超过即跳过执行（pairwise 降维前的保护）


def run_batch(source: Path, functions=None, includes=(), gcc_includes=None,
              defines=None, out_dir: Path = None, exec_limit: int = EXEC_LIMIT,
              clang_extractor: ClangExtractor | None = None,
              include_config: Path | None = None) -> list:
    source = Path(source)
    out_dir = Path(out_dir or ".build/batch")
    # gcc 用系统真 libc：自动剔除解析用的 libc_stub 假头目录
    if gcc_includes is None:
        gcc_includes = [d for d in includes if not str(d).rstrip("/\\").endswith("libc_stub")]
    clang_extractor = clang_extractor or ClangExtractor(default_clang_extractor())
    context = make_compile_context(
        discover_compile_sources(source.parent, source), includes, defines,
        [include_config] if include_config else (),
    )
    extracted = {}
    if not functions:
        extracted = {
            ir.name: ir for ir in clang_extractor.extract_all(
                context, cwd=source.parent
            )
            if Path(ir.file).resolve() == source.resolve()
        }
        functions = list(extracted)
    else:
        targets = tuple((source, fn) for fn in functions)
        extracted = {
            fn: ir for (target_source, fn), ir in
            clang_extractor.extract_targets(context, targets, cwd=source.parent)
            .items()
            if target_source == source.resolve()
        }

    results = []
    for fn in functions:
        rec = {"function": fn, "status": None, "note": ""}
        try:
            ir = extracted[fn]
            rec.update(
                params=len(ir.params),
                ptr_params=[p.name for p in ir.params if p.is_ptr],
                stubs=[c.callee for c in ir.calls],
                stub_cnt=len(ir.calls),
                branches=len(ir.branches),
                atoms=sum(len(b.atoms) for b in ir.branches),
            )
            cand = boundary.control_candidates(ir)
            columns = build_columns(ir, cand)
            cols, rows = boundary.enumerate_rows(ir)
            rec["rows"] = len(rows)
            if len(rows) > exec_limit:
                rec["status"] = "UNSUPPORTED"
                rec["note"] = f"rows={len(rows)} 超执行上限，待 pairwise 降维"
                results.append(rec)
                continue
            driver_code = driver_gen.render_driver(ir, columns, cols, rows)
            harness = extract.build_harness_source(ir, driver_code)
            d = out_dir / fn
            d.mkdir(parents=True, exist_ok=True)
            c_file = d / "harness.c"
            c_file.write_text(harness, encoding="utf-8")
            lines = run.compile_and_run(c_file, d, gcc_includes, defines)
            if len(lines) == len(rows):
                if not rows:
                    rec["status"] = "NEEDS_REVIEW"
                    rec["note"] = "链路通，但无可设定控制变量组合（深层配置依赖），执行 0 用例"
                else:
                    rec["status"] = "VALIDATED"
            else:
                rec["status"] = "EXECUTION_FAILED"
                rec["note"] = f"输出行数 {len(lines)} != 用例数 {len(rows)}"
        except driver_gen.UnsupportedGen as e:
            rec["status"] = "UNSUPPORTED"
            rec["note"] = str(e)[:120]
        except RuntimeError as e:
            msg_lines = str(e).splitlines()
            err = next((l for l in msg_lines if "error:" in l or "错误" in l),
                       msg_lines[1] if len(msg_lines) > 1 else str(e)[:150])
            head = msg_lines[0] if msg_lines else ""
            if "gcc" in head or "编译" in head:
                rec["status"] = "EXECUTION_FAILED"
                rec["note"] = err.strip()[:150]
            else:
                rec["status"] = "UNSUPPORTED"
                rec["note"] = head[:160]
        except Exception as e:  # noqa: BLE001 —— 批量探针必须吞掉单函数异常继续跑
            rec["status"] = "UNSUPPORTED"
            rec["note"] = f"{type(e).__name__}: {e}"[:160]
        results.append(rec)
    return results


def format_table(results: list) -> str:
    head = (f"{'函数':32s} {'状态':12s} {'参数':>4s} {'指针参数':18s} "
            f"{'stub':>4s} {'分支':>4s} {'原子':>4s} {'用例':>6s}  备注")
    lines = [head, "-" * len(head)]
    for r in results:
        lines.append(
            f"{r['function']:32s} {r['status'] or '-':12s} {r.get('params', '-'):>4} "
            f"{','.join(r.get('ptr_params', [])) or '-':18s} "
            f"{r.get('stub_cnt', '-'):>4} {r.get('branches', '-'):>4} "
            f"{r.get('atoms', '-'):>4} {r.get('rows', '-'):>6}  {r.get('note', '')}")
    return "\n".join(lines)
