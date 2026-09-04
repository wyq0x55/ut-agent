"""Re-extract selected legacy evidence targets with the pinned FunctionIR v3 extractor.

Input is an existing generated tree whose old JSON documents identify the target
source/function pair and preserve the project's CompileContext.  The script
remaps only the old checkout prefix to the supplied local project root, then
invokes the C++ extractor for all selected targets in one run.  It never reads
C source text or CSV values itself and refuses duplicate targets.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ut_agent.ir.codec import serialize_document
from ut_agent.toolchain import ClangExtractor, CompileContext


def _remap(value: str, old_root: str, new_root: Path) -> str:
    old = old_root.rstrip("\\/")
    if value == old or value.startswith(old + "\\") or value.startswith(old + "/"):
        return str(new_root / value[len(old):].lstrip("\\/"))
    return value


def _remap_path(value: str, old_root: str, new_root: Path) -> Path:
    return Path(_remap(str(value), old_root, new_root)).resolve()


def _context(raw: dict[str, Any], old_root: str, new_root: Path) -> CompileContext:
    context = raw["compile_context"]
    return CompileContext(
        source_files=tuple(
            _remap_path(item, old_root, new_root)
            for item in context["source_files"]
        ),
        include_dirs=tuple(
            _remap_path(item, old_root, new_root)
            for item in context.get("include_dirs", [])
        ),
        defines=tuple(sorted(
            (str(key), str(value))
            for key, value in context.get("defines", {}).items()
        )),
        force_includes=tuple(
            _remap_path(item, old_root, new_root)
            for item in context.get("force_includes", [])
        ),
        standard=str(context.get("standard", "c11")),
        target_triple=context.get("target_triple"),
        cpu=context.get("cpu"),
        abi=context.get("abi"),
        sysroot=context.get("sysroot"),
        resource_dir=context.get("resource_dir"),
        extra_args=tuple(str(item) for item in context.get("extra_args", [])),
    )


def reextract(
    generated_root: Path,
    project_root: Path,
    extractor_path: Path,
    old_root: str,
    output_root: Path,
) -> dict[str, Any]:
    files = sorted(generated_root.rglob("function-ir.json"),
                   key=lambda item: item.as_posix().lower())
    if not files:
        raise ValueError(f"no function-ir.json under {generated_root}")
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    first = documents[0]
    context = _context(first, old_root, project_root)
    targets: list[tuple[Path, str]] = []
    for document in documents:
        functions = document.get("functions", [])
        if len(functions) != 1:
            raise ValueError(f"expected one function in legacy document: {document}")
        function = functions[0]
        target = (_remap_path(function["file"], old_root, project_root),
                  str(function["name"]))
        targets.append(target)
    if len(set(targets)) != len(targets):
        raise ValueError("duplicate source/function targets")
    missing = [str(source) for source, _ in targets if not source.is_file()]
    if missing:
        raise FileNotFoundError(f"target source does not exist: {missing[0]}")

    extractor = ClangExtractor(Path(extractor_path).resolve(), timeout=900.0)
    extracted = extractor.extract_targets(context, targets, cwd=project_root)
    output_root.mkdir(parents=True, exist_ok=True)
    written = []
    for source, name in targets:
        ir = extracted[(source.resolve(), name)]
        relative = source.relative_to(project_root)
        target_path = output_root / relative / name / "function-ir.json"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            serialize_document(ir.to_dict()), encoding="utf-8", newline="\n"
        )
        written.append(str(target_path))
    return {"targets": len(targets), "output_root": str(output_root.resolve()),
            "files": written}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated-root", required=True, type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--extractor", required=True, type=Path)
    parser.add_argument("--old-root", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    result = reextract(
        args.generated_root.resolve(), args.project_root.resolve(),
        args.extractor.resolve(), args.old_root, args.output_root.resolve()
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
