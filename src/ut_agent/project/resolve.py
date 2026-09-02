"""Resolve a project manifest without implicit baseline upgrades."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ut_agent.baseline import load_baseline
from ut_agent.baseline.loader import load_mapping
from .manifest import load_manifest
from .model import ResolvedProjectContext


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_component(value: str, label: str) -> str:
    if not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(f"{label} 包含非法路径字符: {value!r}")
    return value


def _contract_path(root: Path, directory: str, identifier: str, version: str) -> Path:
    _safe_component(identifier, "contract id")
    _safe_component(version, "contract version")
    return root / directory / identifier / f"{version}.yaml"


def _load_project_rule_pack(path: Path, expected_id: str, expected_version: str) -> dict[str, Any]:
    raw = load_mapping(path)
    payload = raw.get("project_rule_pack", raw)
    if not isinstance(payload, dict):
        raise ValueError(f"ProjectRulePack 顶层必须是 object: {path}")
    if payload.get("id") != expected_id or str(payload.get("version")) != expected_version:
        raise ValueError(f"ProjectRulePack ref 与 manifest 不一致: {path}")
    if payload.get("status") != "approved":
        raise ValueError(f"正式生成只接受 approved ProjectRulePack: {expected_id}@{expected_version}")
    rules = payload.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError(f"ProjectRulePack.rules 必须是 array: {path}")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError(f"ProjectRulePack rule 必须是 object: {path}")
        missing = {"id", "status", "scope", "match", "action", "approval"} - rule.keys()
        if missing:
            raise ValueError(f"ProjectRulePack rule 缺少字段: {sorted(missing)}")
        if rule.get("status") != "approved":
            raise ValueError(
                f"正式生成只接受 approved project rule: {rule.get('id')}"
            )
        if not isinstance(rule.get("approval"), dict) or not rule["approval"]:
            raise ValueError(f"approved project rule 缺少 approval: {rule.get('id')}")
        if not isinstance(rule.get("scope"), dict) or not isinstance(rule.get("match"), dict):
            raise ValueError(f"ProjectRulePack rule scope/match 必须是 object: {rule.get('id')}")
        if not isinstance(rule.get("action"), dict):
            raise ValueError(f"ProjectRulePack rule action 必须是 object: {rule.get('id')}")
        action_keys = set(rule["action"])
        allowed_actions = {
            "add_boundary_value", "restrict_domain", "add_precondition",
            "define_scenario", "mark_unsupported",
        }
        if not action_keys or not action_keys <= allowed_actions:
            raise ValueError(
                f"ProjectRulePack rule action 非 typed semantic action: {rule.get('id')}"
            )
    schema_path = Path(__file__).resolve().parents[3] / "schemas" / "project-rule-pack.schema.json"
    try:
        from jsonschema import Draft202012Validator
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(raw)
    except FileNotFoundError as exc:
        raise ValueError(f"ProjectRulePack schema 不存在: {schema_path}") from exc
    return raw


def resolve_project_context(manifest_path: Path, *, config_root: Path | None = None) -> ResolvedProjectContext:
    manifest = load_manifest(Path(manifest_path))
    root = Path(config_root).resolve() if config_root else Path(__file__).resolve().parents[3] / "config"
    baseline_path = _contract_path(
        root, "baselines", manifest.baseline_id, manifest.baseline_version
    )
    if not baseline_path.is_file():
        raise FileNotFoundError(
            f"未找到 manifest 锁定的 TestBaseline {manifest.baseline_ref}: {baseline_path}"
        )
    baseline = load_baseline(baseline_path)
    if baseline.id != manifest.baseline_id or baseline.version != manifest.baseline_version:
        raise ValueError(
            f"TestBaseline 内容与 manifest 不一致: {manifest.baseline_ref}"
        )
    project_pack = None
    if manifest.project_rule_pack:
        pack_ref = manifest.project_rule_pack
        pack_path = _contract_path(root, "project-rules", pack_ref.id, pack_ref.version)
        if not pack_path.is_file():
            raise FileNotFoundError(f"未找到 ProjectRulePack {pack_ref.ref}: {pack_path}")
        project_pack = _load_project_rule_pack(pack_path, pack_ref.id, pack_ref.version)
    return ResolvedProjectContext(manifest=manifest, baseline=baseline,
                                  project_rule_pack=project_pack)
