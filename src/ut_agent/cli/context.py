"""Shared CLI input normalization; no generation policy lives here."""
from __future__ import annotations

from pathlib import Path

from ut_agent.toolchain import ClangExtractor, discover_compile_sources


def parse_defines(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or []:
        key, _, value = item.partition("=")
        if not key:
            raise ValueError("宏定义名称不能为空")
        result[key] = value
    return result


def source_context_root(source: Path) -> Path:
    """Use the nearest ``src`` ancestor for deterministic source discovery."""
    source = Path(source).resolve()
    for parent in (source.parent, *source.parents):
        if parent.name.lower() == "src":
            return parent
    return source.parent


def compile_sources(source: Path, context_sources: list[str] | None = None,
                    *, discover: bool = True) -> tuple[Path, ...]:
    source = Path(source).resolve()
    explicit = tuple(Path(item).resolve() for item in (context_sources or []))
    if explicit:
        return (source, *(item for item in explicit if item != source))
    if discover:
        return discover_compile_sources(source_context_root(source), source)
    return (source,)


def extractor(path: str | None) -> ClangExtractor:
    from ut_agent.toolchain import default_clang_extractor

    return ClangExtractor(Path(path) if path else default_clang_extractor())
