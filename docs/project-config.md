# 项目配置与版本锁

项目 runtime 配置的唯一组合入口是 `config/projects/<project>.json`。它绑定基础 TestBaseline 的 id/version，并保存项目级开关、构建上下文、WinAMS profile 和可选的已审批项目规则包。

## 配置层次

```text
config/baselines/<id>/<version>.yaml
    Base TestBaseline：基础测试规则、来源、审批状态

config/projects/<project>.json
    ProjectManifest：baseline version lock、项目级 MC/DC、build、WinAMS、规则包引用

config/project-rules/<id>/<version>.json
    可选 ProjectRulePack：有证据且已审批的项目例外/附加规则

config/winams/standard.yaml
    WinAMS 目标格式 profile
```

当前项目 manifest 的最小形态如下；完整规则内容只维护在对应 config 文件中：

```json
{
  "project": {"id": "N-O2608-PSD-087"},
  "baseline": {"id": "psd-rebuild", "version": "1.0"},
  "profile": {"mcdc_enabled": true},
  "rules": {"project_pack": null},
  "build": {"profile": "rh850-ghs"},
  "winams": {"profile": "standard"}
}
```

基础基准不重复保存项目级 MC/DC、基础 profile 名称、profile version 或项目例外。ProjectManifest 是项目 baseline/version lock 的唯一事实源；语料 manifest 只指向它，不复制 baseline ref。

## 来源证据与 runtime baseline

`docs/baselines/psd-rebuild-v1.6/` 和原始 Excel 保存来源、日文单元格、转录范围及 `needs_review/source_only` 状态。它们是人工复核输入，不会因为存在转录文件就自动变成 approved runtime rule。正式生成只加载 `config/baselines/` 中经过审批的版本。

## provenance 与 fail closed

解析后的 `ResolvedProjectContext.provenance` 至少稳定记录：

- project id；
- baseline id、version 和 ref；
- project-level `mcdc_enabled`；
- ProjectRulePack id/version（没有则为 `none`）；
- FunctionIR 和 generator version。

缺失 baseline、版本不匹配、规则包未审批或 schema 不通过时停止正式生成。未知源码事实和未完成证据进入 review 状态，不能用默认值或占位产物伪装有效。

## Runtime baseline approval

An `approved` runtime baseline carries auditable approval metadata in the
baseline document: authority, approver, decision date, scope, reason, and
non-empty evidence references. The current decision record is
[psd-rebuild@1.0 approval](baselines/psd-rebuild-v1.6/approval.md).

The approval record is separate from source evidence. The source manifest may
remain `source_only` / `needs_review`; those statuses are never promoted by
the loader or by documentation cleanup.
