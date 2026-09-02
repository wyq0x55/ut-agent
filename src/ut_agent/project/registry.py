"""Project-to-baseline registry for version-locking checks."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from ut_agent.baseline.loader import load_mapping


@dataclass(frozen=True)
class ProjectBaselineBinding:
    project_id: str
    baseline_id: str
    baseline_version: str
    base_profile: str = ""
    profile_version: str = ""
    mcdc_enabled: bool | None = None
    approved_exceptions: tuple[str, ...] = ()

    @property
    def baseline_ref(self) -> str:
        return f"{self.baseline_id}@{self.baseline_version}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["approved_exceptions"] = list(self.approved_exceptions)
        value["baseline"] = self.baseline_ref
        return value


def _parse_ref(value: Any, project_id: str) -> tuple[str, str]:
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError(
            f"项目 {project_id} 的 baseline 必须使用 id@version 格式"
        )
    identifier, version = value.rsplit("@", 1)
    if not identifier or not version:
        raise ValueError(f"项目 {project_id} 的 baseline id/version 不能为空")
    return identifier, version


def load_project_baselines(path: Path) -> dict[str, ProjectBaselineBinding]:
    """Load a registry without allowing an implicit baseline upgrade."""
    raw = load_mapping(Path(path))
    projects = raw.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise ValueError(f"project-baselines 顶层必须包含非空 projects: {path}")
    result: dict[str, ProjectBaselineBinding] = {}
    for project_id, item in projects.items():
        if not isinstance(item, dict):
            raise ValueError(f"项目 {project_id} 配置必须是 object")
        project_id = str(project_id)
        baseline_id, baseline_version = _parse_ref(item.get("baseline"), project_id)
        exceptions = item.get("approved_exceptions", [])
        if not isinstance(exceptions, list):
            raise ValueError(f"项目 {project_id} approved_exceptions 必须是 array")
        mcdc = item.get("mcdc_enabled")
        if mcdc is not None and not isinstance(mcdc, bool):
            raise ValueError(f"项目 {project_id} mcdc_enabled 必须是 boolean")
        result[project_id] = ProjectBaselineBinding(
            project_id=project_id,
            baseline_id=baseline_id,
            baseline_version=baseline_version,
            base_profile=str(item.get("base_profile", "")),
            profile_version=str(item.get("profile_version", "")),
            mcdc_enabled=mcdc,
            approved_exceptions=tuple(str(value) for value in exceptions),
        )
    return result


def project_baseline(path: Path, project_id: str) -> ProjectBaselineBinding:
    try:
        return load_project_baselines(path)[project_id]
    except KeyError as exc:
        raise KeyError(f"project-baselines 未登记项目: {project_id}") from exc


__all__ = [
    "ProjectBaselineBinding", "load_project_baselines", "project_baseline",
]
