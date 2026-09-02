"""CLI handlers for typed extraction and external toolchain operations."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .context import compile_sources, extractor, parse_defines


def run_parse(args) -> int:
    from ut_agent.ir.codec import serialize_document
    from ut_agent.toolchain import make_compile_context

    source = Path(args.source)
    context = make_compile_context(
        compile_sources(source, args.context_source, discover=not args.list),
        args.include, parse_defines(args.define),
        [Path(args.include_config)] if args.include_config else (),
    )
    client = extractor(args.clang_extractor)
    if args.list:
        for function in client.extract_all(context, cwd=source.parent):
            if Path(function.file).resolve() == source.resolve():
                print(f"{function.name}:{function.line}")
        return 0
    if not args.function:
        print("parse requires --function unless --list is used", file=sys.stderr)
        return 2
    ir = client.extract_from_source(context, args.function, source, cwd=source.parent)
    output = serialize_document(ir.to_dict())
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    atoms = sum(len(branch.atoms) for branch in ir.branches)
    print(
        f"[summary] {ir.name}: params={len(ir.params)} "
        f"calls={[call.callee for call in ir.calls]} branches={len(ir.branches)} atoms={atoms}",
        file=sys.stderr,
    )
    return 0


def run_arm_build(args) -> int:
    from ut_agent.toolchain.arm_gcc import (
        ArmGccConfig, build_elf, convert_to_winams_omf, find_arm_gcc,
    )

    compiler = find_arm_gcc(args.compiler)
    config = ArmGccConfig(compiler=compiler, cpu=args.cpu)
    output, objects = build_elf(
        [Path(item) for item in args.source], Path(args.output), config,
        include_dirs=args.include, defines=parse_defines(args.define), entry=args.entry,
        allow_unresolved=not args.strict_link,
    )
    print(f"[arm-build] {output} objects={len(objects)}", file=sys.stderr)
    if args.omf_output:
        omf = convert_to_winams_omf(
            output, Path(args.omf_output), args.omf_converter, args.dwarf_version
        )
        print(f"[arm-build] {omf} (WinAMS OMF)", file=sys.stderr)
    return 0
