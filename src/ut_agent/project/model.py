"""Typed project manifest and resolved project-context models."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from ut_agent.baseline.model import TestBaseline


@dataclass(frozen=True)
class ProjectRulePackRef:
    id: str
    version: str

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass(frozen=True)
class BuildContext:
    profile: str = "default"
    config: str | None = None


@dataclass(frozen=True)
class TargetProfile:
    name: str = "standard"


@dataclass(frozen=True)
class ProjectManifest:
    project_id: str
    baseline_id: str
    baseline_version: str
    targets: dict[str, Any] = field(default_factory=dict)
    project_rule_pack: ProjectRulePackRef | None = None
    build: BuildContext = field(default_factory=BuildContext)
    target: TargetProfile = field(default_factory=TargetProfile)
    profile: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, source_path: str | None = None):
        project = value.get("project", value)
        baseline = value.get("baseline")
        if not isinstance(project, Mapping) or not isinstance(baseline, Mapping):
            raise ValueError("ProjectManifest 必须包含 project 和 baseline object")
        required = {"id"} - set(project)
        required |= {"id", "version"} - set(baseline)
        if required:
            raise ValueError(f"ProjectManifest 缺少字段: {sorted(required)}")
        rules = value.get("rules", {})
        pack = None
        if rules is not None:
            if not isinstance(rules, Mapping):
                raise ValueError("ProjectManifest.rules 必须是 object")
            raw_pack = rules.get("project_pack")
            if raw_pack:
                if not isinstance(raw_pack, str) or "@" not in raw_pack:
                    raise ValueError("project_pack 必须使用 id@version 格式")
                pack_id, pack_version = raw_pack.rsplit("@", 1)
                if not pack_id or not pack_version:
                    raise ValueError("project_pack id/version 不能为空")
                pack = ProjectRulePackRef(pack_id, pack_version)
        build_raw = value.get("build", {})
        winams_raw = value.get("winams", {})
        profile_raw = value.get("profile", {})
        if (not isinstance(build_raw, Mapping)
                or not isinstance(winams_raw, Mapping)
                or not isinstance(profile_raw, Mapping)):
            raise ValueError("build/winams/profile 必须是 object")
        allowed_profile_keys = {"mcdc_enabled", "approved_exceptions"}
        unknown_profile_keys = set(profile_raw) - allowed_profile_keys
        if unknown_profile_keys:
            raise ValueError(
                "ProjectManifest.profile 不允许重复基础基准字段: "
                f"{sorted(unknown_profile_keys)}"
            )
        mcdc_enabled = profile_raw.get("mcdc_enabled", False)
        if not isinstance(mcdc_enabled, bool):
            raise ValueError("ProjectManifest.profile.mcdc_enabled 必须是 boolean")
        exceptions = profile_raw.get("approved_exceptions", [])
        if not isinstance(exceptions, list) or any(
            not isinstance(item, str) for item in exceptions
        ):
            raise ValueError(
                "ProjectManifest.profile.approved_exceptions 必须是 string array"
            )
        return cls(
            project_id=str(project["id"]),
            baseline_id=str(baseline["id"]),
            baseline_version=str(baseline["version"]),
            targets=dict(value.get("targets", {})),
            project_rule_pack=pack,
            build=BuildContext(
                profile=str(build_raw.get("profile", "default")),
                config=(str(build_raw["config"]) if build_raw.get("config") else None),
            ),
            target=TargetProfile(name=str(winams_raw.get("profile", "standard"))),
            profile=dict(profile_raw),
            source_path=source_path,
        )

    @property
    def baseline_ref(self) -> str:
        return f"{self.baseline_id}@{self.baseline_version}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["project"] = {"id": self.project_id}
        data["baseline"] = {"id": self.baseline_id, "version": self.baseline_version}
        data["rules"] = {
            "project_pack": self.project_rule_pack.ref if self.project_rule_pack else None,
        }
        data["winams"] = {"profile": self.target.name}
        data.pop("project_id", None)
        data.pop("baseline_id", None)
        data.pop("baseline_version", None)
        data.pop("project_rule_pack", None)
        data.pop("target", None)
        data.pop("source_path", None)
        return data


@dataclass(frozen=True)
class ResolvedProjectContext:
    manifest: ProjectManifest
    baseline: TestBaseline
    project_rule_pack: dict[str, Any] | None = None
    function_ir_version: int = 3
    generator_version: str = "0.1.0"

    @property
    def project_id(self) -> str:
        return self.manifest.project_id

    @property
    def baseline_ref(self) -> str:
        return self.baseline.ref

    @property
    def provenance(self) -> dict[str, Any]:
        profile = self.manifest.profile
        return {
            "project_id": self.project_id,
            "baseline_id": self.baseline.id,
            "baseline_version": self.baseline.version,
            "baseline_ref": self.baseline.ref,
            "baseline_approval": dict(self.baseline.approval),
            "project_rule_pack_version": (
                self.manifest.project_rule_pack.version
                if self.manifest.project_rule_pack else "none"
            ),
            "project_rule_pack_id": (
                self.manifest.project_rule_pack.id
                if self.manifest.project_rule_pack else "none"
            ),
            "mcdc_enabled": bool(profile.get("mcdc_enabled", False)),
            "approved_exceptions": tuple(
                str(item) for item in profile.get("approved_exceptions", [])
            ),
            "function_ir_version": self.function_ir_version,
            "generator_version": self.generator_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "baseline": self.baseline.to_dict(),
            "project_rule_pack": self.project_rule_pack,
            "provenance": self.provenance,
        }
