"""Compatibility entry points for the offline corpus workflow.

Source discovery and extractor invocation live in :mod:`ut_agent.parser`.
This module intentionally contains no source access or semantic extraction;
the lazy forwarding keeps older callers from importing the parser at package
initialization time.
"""
from __future__ import annotations

from typing import Any


def collect_rule_corpus(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from ut_agent.parser.rule_corpus import collect_rule_corpus as collect

    return collect(*args, **kwargs)


def discover_samples(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    from ut_agent.parser.rule_corpus import discover_samples as discover

    return discover(*args, **kwargs)


def infer_source_root(*args: Any, **kwargs: Any):
    from ut_agent.parser.rule_corpus import infer_source_root as infer

    return infer(*args, **kwargs)


def discover_include_dirs(*args: Any, **kwargs: Any):
    from ut_agent.parser.rule_corpus import discover_include_dirs as discover

    return discover(*args, **kwargs)


__all__ = [
    "collect_rule_corpus", "discover_samples", "infer_source_root",
    "discover_include_dirs",
]
