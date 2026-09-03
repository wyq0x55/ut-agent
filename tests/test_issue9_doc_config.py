"""Issue #9 documentation and single-source configuration gates."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

from check_docs import (  # noqa: E402
    check_cli_documentation,
    check_config_single_source,
    check_current_forbidden_terms,
    check_markdown_links,
)
from ut_agent.baseline import load_baseline  # noqa: E402
from ut_agent.baseline.loader import load_mapping  # noqa: E402
from ut_agent.project import resolve_project_context  # noqa: E402
from ut_agent.reporting import load_corpus_manifest  # noqa: E402


def test_issue9_current_docs_cli_and_config_gates_pass():
    assert check_markdown_links(ROOT) == []
    assert check_current_forbidden_terms(ROOT) == []
    assert check_cli_documentation(ROOT) == []
    assert check_config_single_source(ROOT) == []


def test_issue9_runtime_baseline_and_corpus_have_one_source():
    baseline_path = ROOT / "config" / "baselines" / "psd-rebuild" / "1.0.yaml"
    baseline = load_mapping(baseline_path)["baseline"]
    assert baseline["id"] == "psd-rebuild"
    approval = baseline["approval"]
    assert approval["authority"] == "repository-owner"
    assert approval["approved_by"] == "wyq0x55"
    assert approval["approved_at"] == "2026-09-03"
    assert approval["scope"] == "baseline-and-rules"
    assert approval["evidence"]
    assert (ROOT / "docs" / "baselines" / "psd-rebuild-v1.6" / "approval.md").is_file()
    context = resolve_project_context(ROOT / "config" / "projects" / "N-O2608-PSD-087.json")
    assert context.provenance["baseline_approval"]["approved_by"] == "wyq0x55"
    assert not {
        "base_profile", "mcdc_enabled", "approved_exceptions",
    }.intersection(baseline)
    legacy_baseline_dir = ROOT / "config" / "baselines" / ("psd-rebuild" + "-mcdc")
    assert not legacy_baseline_dir.exists()
    assert not (ROOT / "config" / "projects" / "project-baselines.yaml").exists()

    corpus_path = ROOT / "config" / "projects" / "N-O2608-PSD-087.corpus.json"
    raw = json.loads(corpus_path.read_text(encoding="utf-8"))
    assert "baseline" not in raw["project"]
    manifest = load_corpus_manifest(corpus_path)
    assert "baseline" not in manifest.to_dict()["project"]


def test_issue9_approved_baseline_requires_approval_evidence(tmp_path: Path):
    path = tmp_path / "without-approval.yaml"
    path.write_text(
        "baseline:\n"
        "  id: synthetic\n"
        "  version: '1.0'\n"
        "  status: approved\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_baseline(path)


def test_issue9_legacy_project_policy_is_rejected(tmp_path: Path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({
        "project": {"id": "legacy"},
        "baseline": {"id": "psd-rebuild", "version": "1.0"},
        "profile": {"base_profile": "PSD再構築"},
    }), encoding="utf-8")
    from ut_agent.project import load_manifest

    with pytest.raises(Exception):
        load_manifest(path)
