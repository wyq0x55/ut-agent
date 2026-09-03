"""Deterministic documentation, CLI, and config single-source gate.

The checker is intentionally local-only.  It validates the repository's
current documentation and runtime configuration without reading Golden data,
calling the network, or modifying files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
CURRENT_FIXED_DOCS = (
    Path("README.md"),
    Path("AGENTS.md"),
    Path("docs/README.md"),
    Path("docs/architecture.md"),
    Path("docs/project-config.md"),
    Path("docs/corpus-validation.md"),
)
CURRENT_FORBIDDEN_TERMS = (
    "ResolvedTestPolicy",
    "RequirementModule",
    "Requirement Modules",
    "config/requirements",
    "psd-rebuild-mcdc",
    "docs/用例表与CSV格式规格",
    "docs/确定性规则引擎.md",
)

_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\n]+)\)")
_CLI_COMMAND = re.compile(r"\but-agent\s+([a-z][a-z0-9-]*)\b")


def current_markdown_paths(root: Path = ROOT) -> tuple[Path, ...]:
    """Return current Markdown docs, excluding historical archive material."""
    root = Path(root)
    paths = [root / relative for relative in CURRENT_FIXED_DOCS]
    winams = root / "docs" / "winams"
    if winams.is_dir():
        paths.extend(winams.rglob("*.md"))
    return tuple(sorted(set(paths), key=lambda item: item.as_posix().lower()))


def _link_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1:value.index(">")]
    return value.split(None, 1)[0]


def check_markdown_links(root: Path = ROOT) -> list[str]:
    """Check repo-local Markdown targets in current docs."""
    root = Path(root).resolve()
    violations: list[str] = []
    for path in current_markdown_paths(root):
        if not path.is_file():
            violations.append(f"missing current document: {path.relative_to(root)}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            violations.append(f"unreadable document {path.relative_to(root)}: {exc}")
            continue
        for match in _MARKDOWN_LINK.finditer(text):
            target = unquote(_link_target(match.group(1)))
            path_part = target.split("#", 1)[0].split("?", 1)[0]
            if not path_part or target.startswith(("#", "http://", "https://", "mailto:", "tel:")):
                continue
            if path_part.startswith("/") and not re.match(r"^/[A-Za-z]:", path_part):
                candidate = root / path_part.lstrip("/")
            else:
                candidate = (path.parent / path_part).resolve()
            try:
                inside_root = candidate == root or root in candidate.parents
            except (OSError, ValueError):
                inside_root = False
            if not inside_root or not candidate.exists():
                line = text.count("\n", 0, match.start()) + 1
                violations.append(
                    f"broken repo-local link {path.relative_to(root)}:{line}: {target}"
                )
    return violations


def _current_text_paths(root: Path) -> tuple[Path, ...]:
    paths = list(current_markdown_paths(root))
    config = Path(root) / "config"
    if config.is_dir():
        paths.extend(item for item in config.rglob("*") if item.is_file())
    return tuple(sorted(set(paths), key=lambda item: item.as_posix().lower()))


def check_current_forbidden_terms(root: Path = ROOT) -> list[str]:
    """Reject historical/current-model terms in normative docs and config."""
    root = Path(root).resolve()
    violations: list[str] = []
    for path in _current_text_paths(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(root)
        for term in CURRENT_FORBIDDEN_TERMS:
            start = 0
            while True:
                offset = text.find(term, start)
                if offset < 0:
                    break
                line = text.count("\n", 0, offset) + 1
                violations.append(f"forbidden current-doc term {relative}:{line}: {term}")
                start = offset + len(term)
    return violations


def _top_level_commands() -> set[str]:
    from ut_agent.cli.parser import build_parser

    parser = build_parser()
    for action in parser._actions:  # argparse exposes no public subparser API.
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def check_cli_documentation(root: Path = ROOT) -> list[str]:
    """Ensure every top-level command mentioned by README is real argparse."""
    root = Path(root).resolve()
    readme = root / "README.md"
    if not readme.is_file():
        return ["README.md is missing"]
    try:
        text = readme.read_text(encoding="utf-8")
        sys.path.insert(0, str(root / "src"))
        commands = _top_level_commands()
    except (OSError, UnicodeError, ImportError, RuntimeError) as exc:
        return [f"cannot inspect CLI parser: {exc}"]
    documented = sorted(set(_CLI_COMMAND.findall(text)))
    return [
        f"README documents unknown CLI command: {command}"
        for command in documented if command not in commands
    ]


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON top level must be object: {path}")
    return value


def check_config_single_source(root: Path = ROOT) -> list[str]:
    """Check baseline/project/corpus config has one runtime source of truth."""
    root = Path(root).resolve()
    config = root / "config"
    violations: list[str] = []
    duplicate_registry = config / "projects" / "project-baselines.yaml"
    if duplicate_registry.exists():
        violations.append(f"duplicate project baseline registry exists: {duplicate_registry.relative_to(root)}")
    legacy_baseline_dir = config / "baselines" / "psd-rebuild-mcdc"
    if legacy_baseline_dir.exists():
        violations.append(f"legacy baseline directory exists: {legacy_baseline_dir.relative_to(root)}")

    try:
        sys.path.insert(0, str(root / "src"))
        from ut_agent.baseline.loader import load_mapping
        from ut_agent.project import load_manifest, resolve_project_context
        from ut_agent.reporting import load_corpus_manifest
    except ImportError as exc:
        return [f"cannot import config contracts: {exc}"]

    baseline_root = config / "baselines"
    if baseline_root.is_dir():
        for path in sorted(baseline_root.rglob("*.yaml"), key=lambda item: item.as_posix().lower()):
            try:
                raw = load_mapping(path)
                payload = raw.get("baseline", raw)
                if not isinstance(payload, dict):
                    raise ValueError("baseline payload must be object")
                approval = payload.get("approval")
                required_approval = {
                    "authority", "approved_by", "approved_at", "scope",
                    "reason", "evidence",
                }
                if not isinstance(approval, dict):
                    violations.append(
                        f"approved baseline lacks approval metadata: {path.relative_to(root)}"
                    )
                else:
                    missing_approval = required_approval - approval.keys()
                    if missing_approval:
                        violations.append(
                            f"baseline approval metadata incomplete {path.relative_to(root)}: "
                            f"{sorted(missing_approval)}"
                        )
                    evidence = approval.get("evidence", [])
                    if not isinstance(evidence, list) or not evidence:
                        violations.append(
                            f"baseline approval evidence must be non-empty: {path.relative_to(root)}"
                        )
                    elif any(not isinstance(item, str) or not item.strip() for item in evidence):
                        violations.append(
                            f"baseline approval evidence must contain non-empty strings: "
                            f"{path.relative_to(root)}"
                        )
                    for item in evidence if isinstance(evidence, list) else []:
                        if not isinstance(item, str) or item.startswith(("http://", "https://")):
                            continue
                        evidence_path = (root / item).resolve()
                        if root not in evidence_path.parents or not evidence_path.is_file():
                            violations.append(
                                f"missing baseline approval evidence {path.relative_to(root)}: {item}"
                            )
                repeated = {"base_profile", "mcdc_enabled", "approved_exceptions"}.intersection(payload)
                if repeated:
                    violations.append(
                        f"baseline repeats project policy {path.relative_to(root)}: {sorted(repeated)}"
                    )
                identifier = str(payload.get("id", ""))
                version = str(payload.get("version", ""))
                if identifier == "psd-rebuild-mcdc":
                    violations.append(f"baseline identity contains project switch: {path.relative_to(root)}")
                if identifier and path.parent.name != identifier:
                    violations.append(f"baseline path/id mismatch: {path.relative_to(root)}")
                if version and path.stem != version:
                    violations.append(f"baseline path/version mismatch: {path.relative_to(root)}")
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                violations.append(f"invalid baseline {path.relative_to(root)}: {exc}")

    project_root = config / "projects"
    if project_root.is_dir():
        for path in sorted(project_root.glob("*.json"), key=lambda item: item.name.lower()):
            try:
                raw = _json(path)
                if path.name.endswith(".corpus.json"):
                    project = raw.get("project", {})
                    if "baseline" in project:
                        violations.append(f"corpus repeats baseline: {path.relative_to(root)}")
                    load_corpus_manifest(path)
                    continue
                profile = raw.get("profile", {})
                repeated = {"base_profile", "profile_version"}.intersection(profile)
                if repeated:
                    violations.append(
                        f"project repeats baseline metadata {path.relative_to(root)}: {sorted(repeated)}"
                    )
                manifest = load_manifest(path)
                context = resolve_project_context(path, config_root=config)
                provenance = context.provenance
                required = {
                    "baseline_id", "baseline_version", "baseline_ref", "mcdc_enabled",
                    "baseline_approval", "project_rule_pack_version",
                }
                missing = required - provenance.keys()
                if missing:
                    violations.append(
                        f"project provenance incomplete {path.relative_to(root)}: {sorted(missing)}"
                    )
                if manifest.baseline_id == "psd-rebuild-mcdc":
                    violations.append(f"project identity contains project switch: {path.relative_to(root)}")
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                violations.append(f"invalid project config {path.relative_to(root)}: {exc}")
    return violations


def run(root: Path = ROOT) -> int:
    root = Path(root).resolve()
    checks = (
        ("markdown-links", check_markdown_links(root)),
        ("current-terms", check_current_forbidden_terms(root)),
        ("cli", check_cli_documentation(root)),
        ("config-single-source", check_config_single_source(root)),
    )
    failures = 0
    for name, violations in checks:
        if violations:
            failures += len(violations)
            for violation in violations:
                print(f"[{name}] {violation}", file=sys.stderr)
        else:
            print(f"[{name}] OK")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    return run(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
