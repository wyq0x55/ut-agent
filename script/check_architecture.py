"""Deterministic architecture gate for the Issue #3 domain split.

Usage: ``python script/check_architecture.py``.  The checker only inspects
Python import declarations and tracked source locations; it does not import
the production packages or execute generation code.
"""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ut_agent"
OLD_FILES = (
    "ir.py", "cli.py",
)
OLD_DIRS = ("parser", "rules", "cases", "winams", "host", "stub", "artifacts")
FORBIDDEN = {
    "generation": ("ut_agent.learning", "ut_agent.targets.winams", "ut_agent.toolchain"),
    "baseline": ("ut_agent.generation", "ut_agent.targets.winams"),
    "ir": ("ut_agent.baseline", "ut_agent.generation", "ut_agent.targets.winams"),
    "project": ("ut_agent.targets.winams",),
    "targets/winams": ("ut_agent.learning",),
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def violations() -> list[str]:
    errors: list[str] = []
    for name in OLD_FILES:
        if (SRC / name).exists():
            errors.append(f"old module remains: {SRC / name}")
    for name in OLD_DIRS:
        if any((SRC / name).glob("*.py")):
            errors.append(f"old package remains: {SRC / name}")
    for domain, prefixes in FORBIDDEN.items():
        root = SRC / domain
        for path in sorted(root.glob("*.py")):
            for imported in _imports(path):
                if any(imported == prefix or imported.startswith(prefix + ".")
                       for prefix in prefixes):
                    errors.append(f"{path}: forbidden import {imported}")
    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("\n".join(errors))
        return 1
    print("architecture: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
