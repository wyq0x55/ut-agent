"""Deterministic client for the standalone Clang FunctionIR extractor.

This module deliberately does not parse C source.  It owns only the boundary
between a caller-provided CompileContext and the versioned JSON adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Mapping, Sequence

from ut_agent.ir import FunctionIR
from ut_agent.parser.ir_json import (
    document_to_function_ir,
    read_document,
)


class ClangExtractorError(RuntimeError):
    """The extractor could not produce a usable FunctionIR document."""


def default_clang_extractor() -> Path:
    """Locate the repository-built C++ LibTooling extractor.

    The C++ extractor is the only supported C parser.  A missing binary is a
    configuration/build error; callers must not silently switch to a Python
    parser or a token/regular-expression approximation.
    """
    root = Path(__file__).resolve().parents[3]
    candidates = (
        root / ".build" / "ut-clang-extract-nmake2" / "bin" / "ut-clang-extract.exe",
        root / ".build" / "ut-clang-extract" / "bin" / "ut-clang-extract.exe",
        root / "tooling" / "ut-clang-extract.exe",
    )
    for path in candidates:
        if path.is_file():
            return path
    searched = ", ".join(str(path) for path in candidates)
    raise ClangExtractorError(
        "C++ Clang extractor not found; build tooling/ut-clang-extract first. "
        f"searched: {searched}"
    )


def discover_compile_sources(root: Path, primary: Path | None = None) -> tuple[Path, ...]:
    """Discover C translation units for an AST context, deterministically.

    This is filesystem discovery only.  It deliberately does not inspect C
    source text; semantic discovery remains the responsibility of the C++
    extractor.  ``primary`` is placed first so a caller can select the target
    function from a multi-file extraction without relying on directory order.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"C source context root not found: {root}")
    sources = {
        path.resolve()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".c", ".cc", ".cpp", ".cxx"}
    }
    if primary is not None:
        primary = Path(primary).resolve()
        if not primary.is_file():
            raise FileNotFoundError(f"primary C source not found: {primary}")
        sources.add(primary)
    ordered = sorted(sources, key=lambda path: path.as_posix().lower())
    if primary is None:
        return tuple(ordered)
    return (primary, *(path for path in ordered if path != primary))


@dataclass(frozen=True)
class CompileContext:
    """Explicit inputs needed to reproduce one Clang translation unit."""

    source_files: tuple[Path, ...]
    include_dirs: tuple[Path, ...] = ()
    defines: tuple[tuple[str, str], ...] = ()
    force_includes: tuple[Path, ...] = ()
    standard: str = "c11"
    target_triple: str | None = None
    cpu: str | None = None
    abi: str | None = None
    sysroot: str | None = None
    resource_dir: str | None = None
    # Legacy embedded C often contains pointer-type diagnostics that Clang
    # diagnoses as errors even though the AST is still usable for extraction.
    # Keep the diagnostic in the JSON report, but allow AST construction to
    # continue.  Real syntax/semantic errors remain fatal.
    extra_args: tuple[str, ...] = ("-Wno-error=incompatible-pointer-types",)

    def to_dict(self) -> dict[str, object]:
        if not self.source_files:
            raise ValueError("CompileContext.source_files must not be empty")
        if not self.standard:
            raise ValueError("CompileContext.standard must not be empty")
        return {
            "schema_version": 1,
            "language": "c",
            "standard": self.standard,
            "source_files": [str(path.resolve()) for path in self.source_files],
            "include_dirs": [str(path.resolve()) for path in self.include_dirs],
            "defines": {key: value for key, value in sorted(self.defines)},
            "force_includes": [str(path.resolve()) for path in self.force_includes],
            "target_triple": self.target_triple,
            "cpu": self.cpu,
            "abi": self.abi,
            "sysroot": self.sysroot,
            "resource_dir": self.resource_dir,
            "extra_args": list(self.extra_args),
        }


def make_compile_context(
    source_files: Sequence[Path],
    include_dirs: Sequence[Path] = (),
    defines: Mapping[str, str] | None = None,
    force_includes: Sequence[Path] = (),
    *,
    standard: str = "c11",
    target_triple: str | None = None,
    cpu: str | None = None,
    abi: str | None = None,
    sysroot: str | None = None,
    resource_dir: str | None = None,
    extra_args: Sequence[str] = ("-Wno-error=incompatible-pointer-types",),
) -> CompileContext:
    """Build a normalized, deterministic CompileContext."""

    return CompileContext(
        source_files=tuple(Path(path) for path in source_files),
        include_dirs=tuple(Path(path) for path in include_dirs),
        defines=tuple(sorted((str(key), str(value)) for key, value in (defines or {}).items())),
        force_includes=tuple(Path(path) for path in force_includes),
        standard=standard,
        target_triple=target_triple,
        cpu=cpu,
        abi=abi,
        sysroot=sysroot,
        resource_dir=resource_dir,
        extra_args=tuple(str(value) for value in extra_args),
    )


@dataclass(frozen=True)
class ClangExtractor:
    """Invoke one pinned extractor executable and map its v2 result."""

    executable: Path
    timeout: float = 120.0

    def extract_document(
        self,
        context: CompileContext,
        function_name: str | None = None,
        *,
        cwd: Path | None = None,
        targets: Sequence[tuple[Path, str]] | None = None,
    ) -> dict[str, object]:
        executable = self.executable.resolve()
        if not executable.is_file():
            raise ClangExtractorError(f"extractor executable not found: {executable}")

        run_cwd = Path(cwd).resolve() if cwd is not None else Path.cwd()
        if function_name and targets:
            raise ValueError("function_name and targets are mutually exclusive")
        with tempfile.TemporaryDirectory(prefix="ut-agent-clang-") as directory:
            directory_path = Path(directory)
            context_path = directory_path / "compile-context.json"
            output_path = directory_path / "function-ir-v2.json"
            context_path.write_text(
                json.dumps(
                    context.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            targets_path = None
            if targets is not None:
                if not targets:
                    raise ValueError("targets must not be empty")
                targets_path = directory_path / "targets.tsv"
                targets_path.write_text(
                    "".join(
                        f"{Path(source).resolve()}\t{name}\n"
                        for source, name in targets
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
            command = [
                str(executable),
                "--context",
                str(context_path),
                "--output",
                str(output_path),
            ]
            if function_name:
                command.extend(["--function", function_name])
            if targets_path is not None:
                command.extend(["--targets-file", str(targets_path)])
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(run_cwd),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ClangExtractorError(f"extractor invocation failed: {error}") from error

            if not output_path.is_file():
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise ClangExtractorError(
                    f"extractor produced no JSON (exit={completed.returncode}): {detail}"
                )
            try:
                document = read_document(output_path)
            except Exception as error:
                raise ClangExtractorError(
                    f"extractor JSON is invalid (exit={completed.returncode}): {error}"
                ) from error

        if completed.returncode != 0 or document["status"] == "ERROR":
            detail = "; ".join(
                str(issue["message"]) for issue in document["diagnostics"]
            )
            raise ClangExtractorError(
                f"extractor failed (exit={completed.returncode}, status={document['status']}): "
                f"{detail or 'no diagnostic'}"
            )
        return document

    def extract(
        self,
        context: CompileContext,
        function_name: str,
        *,
        cwd: Path | None = None,
    ) -> FunctionIR:
        if not function_name:
            raise ValueError("function_name must not be empty")
        document = self.extract_document(context, function_name, cwd=cwd)
        if len(document["functions"]) != 1:
            raise ClangExtractorError(
                f"expected one function, got {len(document['functions'])}"
            )
        return document_to_function_ir(document)

    def extract_from_source(
        self,
        context: CompileContext,
        function_name: str,
        source: Path,
        *,
        cwd: Path | None = None,
    ) -> FunctionIR:
        """Extract ``function_name`` from one source in a multi-file context."""
        if not function_name:
            raise ValueError("function_name must not be empty")
        source = Path(source).resolve()
        document = self.extract_document(context, function_name, cwd=cwd)
        matches = [
            function for function in document["functions"]
            if Path(str(function["file"])).resolve() == source
        ]
        if len(matches) != 1:
            raise ClangExtractorError(
                f"expected one {function_name} definition in {source}, got {len(matches)}"
            )
        selected = dict(document)
        selected["functions"] = matches
        return document_to_function_ir(selected)

    def extract_all(
        self,
        context: CompileContext,
        *,
        cwd: Path | None = None,
    ) -> list[FunctionIR]:
        """Extract every source-defined function in stable extractor order."""

        document = self.extract_document(context, cwd=cwd)
        functions = document["functions"]
        result = []
        for function in functions:
            one = dict(document)
            one["functions"] = [function]
            result.append(document_to_function_ir(one))
        return result

    def extract_targets(
        self,
        context: CompileContext,
        targets: Sequence[tuple[Path, str]],
        *,
        cwd: Path | None = None,
    ) -> dict[tuple[Path, str], FunctionIR]:
        """Extract a selected set of functions in one C++ invocation.

        The extractor still parses every translation unit in the supplied
        context so global initializers and function-pointer tables remain
        available, but only the requested function bodies are materialized.
        """
        normalized = tuple((Path(source).resolve(), str(name))
                           for source, name in targets)
        if not normalized:
            raise ValueError("targets must not be empty")
        document = self.extract_document(context, cwd=cwd, targets=normalized)
        result: dict[tuple[Path, str], FunctionIR] = {}
        for function in document["functions"]:
            key = (Path(str(function["file"])).resolve(),
                   str(function["name"]))
            one = dict(document)
            one["functions"] = [function]
            result[key] = document_to_function_ir(one)
        missing = [key for key in normalized if key not in result]
        if missing:
            formatted = ", ".join(f"{source}:{name}" for source, name in missing)
            raise ClangExtractorError(f"requested function targets not found: {formatted}")
        return {key: result[key] for key in normalized}
