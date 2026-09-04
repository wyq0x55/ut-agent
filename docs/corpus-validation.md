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

Golden 只能用于 `learning` 和 corpus validation。比较先建立 target-neutral testcase，按 viewpoint/decision/condition truth vector、strict required values、stub/pre-state 和 oracle 做确定性匹配；自由代表值差异可以归入语义等价，但 Golden replay 还必须满足行数和行顺序。每个 `EXACT_SEMANTIC_MATCH`、`EQUIVALENT_REPRESENTATIVE`、`PARTIAL_MATCH`、`MISSING_GENERATED`、`EXTRA_GENERATED` 和 `AMBIGUOUS_MATCH` 都保留在报告中，并额外报告 `row_count_equal`/`row_order_equal`。

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

候选规则必须先按语义场景聚类。单函数样例只能形成 candidate；只有跨项目且通过 leave-one-project-out 留出验证的 `semantic_family`/`semantic_pattern` 才能批准。函数名、项目名、固定下标和 Golden 具体输入行不能成为通用规则条件。

## 运行方式

```bash
uv sync --extra dev
uv run ut-agent validate-corpus \
  --manifest config/projects/N-O2608-PSD-087.corpus.json \
  --out .tmp/N-O2608-PSD-087
```

缺失的真实客户 corpus 是输入缺口，应在报告中标记为 `BLOCKED`/`FIXTURE_MISSING`；synthetic fixture 继续提交到测试目录。`NEEDS_REVIEW` 也会写出 `csv_kind=partial_candidate` 的 CSV，方便与 Golden 做列结构和已证明 testcase 对比，但不会填充未知值。生成状态 `VALIDATED` 只说明 generation/target validation gate 通过，不等于 WinAMS GUI 或实际执行已经完成。

报告文件为 `.tmp/<project>/project-validation.json` 和 `.tmp/<project>/project-validation.md`。报告中的 gap 只能使用 approved baseline、project switch、FunctionIR、obligation、solver、evaluator、oracle、suite、harness、projection 或 Golden 的实际证据分类；不能按 Golden 与生成数量的大小直接推断根因。
