"""Thin public CLI dispatcher.

Argument parsing and command implementations live in separate modules.  The
dispatcher intentionally contains no generation, extraction, or WinAMS logic.
"""
from __future__ import annotations

import sys

from .parser import build_parser


def _configure_console_encoding() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    if args.cmd == "parse":
        from .extract import run_parse
        return run_parse(args)
    if args.cmd == "arm-build":
        from .extract import run_arm_build
        return run_arm_build(args)
    if args.cmd == "gen":
        from .gen import run
        return run(args)
    if args.cmd == "rules":
        from .rules import run
        return run(args)
    if args.cmd == "batch":
        from .project import run_batch
        return run_batch(args)
    if args.cmd == "psd-project":
        from .project import run_index
        return run_index(args)
    if args.cmd == "project":
        from .project import run_project
        return run_project(args)
    if args.cmd == "artifacts":
        from .project import run_artifacts
        return run_artifacts(args)
    raise AssertionError(f"unhandled command: {args.cmd}")


# Importing ``ut_agent.cli.main`` normally makes Python place the submodule on
# ``ut_agent.cli.main``.  Keep that module callable so the historical
# ``ut_agent.cli.main(...)`` package API remains stable under either import
# order.
import types as _types
import sys as _sys


class _CallableModule(_types.ModuleType):
    def __call__(self, argv=None):
        return self.main(argv)


_sys.modules[__name__].__class__ = _CallableModule


if __name__ == "__main__":
    sys.exit(main())
