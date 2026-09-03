# 语料校验闭环

语料校验是离线的证据闭环，不是正常 generation 的输入通道。项目语料 manifest 通过 `context_manifest` 指向 [ProjectManifest](../config/projects/N-O2608-PSD-087.json)，自身只描述索引、源码/产品目录、Golden 目录、scope 和来源证据。

## 当前流程

```text
Human Golden
    ↓
normalize
    ↓
semantic compare
    ↓
gap taxonomy + owner
    ↓
root-cause fix
    ↓
synthetic fixture + real regression
```

Golden 只能用于 `learning` 和 corpus validation。比较使用语义维度和 semantic signature，不把行顺序或自由代表值差异自动当成源码语义错误；任何不确定差异仍保留为 review gap。

## Manifest 边界

```text
config/projects/<project>.corpus.json
  → context_manifest
  → baseline/version、MC/DC 和项目规则从 ProjectManifest resolve
  → corpus input / Golden / evidence / scope
```

因此 corpus manifest 不重复保存 baseline、MC/DC 或 profile version。结构由 [project-corpus schema](../schemas/project-corpus.schema.json) 约束；项目 context 由 [project-manifest schema](../schemas/project-manifest.schema.json) 约束。

## Gap 分类

差异必须归入真实 owner 层：`BASELINE_GAP`、`PROJECT_RULE_GAP`、`FUNCTION_IR_GAP`、`OBLIGATION_GAP`、`SOLVER_GAP`、`EVALUATOR_GAP`、`ORACLE_GAP`、`SUITE_GAP`、`HARNESS_GAP`、`PROJECTION_GAP` 或 `GOLDEN_ERROR`。不允许用总括性的“生成不一致”掩盖根因，也不允许把 Golden 直接复制进生成结果。

## 运行方式

```bash
ut-agent validate-corpus \
  --manifest config/projects/N-O2608-PSD-087.corpus.json \
  --out .tmp/N-O2608-PSD-087
```

缺失的真实客户 corpus 是输入缺口，应报告或跳过对应 integration/corpus test；synthetic fixture 继续提交到测试目录。生成状态 `VALIDATED` 只说明 generation/target validation gate 通过，不等于 WinAMS GUI 或实际执行已经完成。
