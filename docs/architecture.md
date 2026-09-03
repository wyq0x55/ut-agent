# 当前架构

本页是当前实现的唯一架构说明。生产代码和 schema 优先级高于说明文字；本页不描述未来尚未落地的抽象。

## 端到端数据流

```text
C Source + CompileContext
        ↓
ut-clang-extract (C++/LibTooling)
        ↓
Typed FunctionIR v3
        ↓
ResolvedProjectContext
  ├─ versioned TestBaseline
  ├─ project-level MC/DC switch
  └─ optional approved ProjectRulePack / exceptions
        ↓
Obligation → Constraint → Solve → Evaluate → Oracle → Validate → Suite
        ↓
SemanticTestSuite
        ↓
targets/winams
        ↓
TestCsv / Stub / DefineVar / Harness
```

`ut-clang-extract` 是唯一的 C semantic fact producer。它负责从源码和 CompileContext 提取类型、控制流、调用、数据流、全局对象和 provenance；Python 生产代码只消费 typed FunctionIR，不重新解析源码或猜测 C 语义。

## 领域职责

- `ir/`：FunctionIR v3 dataclass、JSON codec 和 schema validation。
- `baseline/`：只表达 versioned Base TestBaseline 的规则、来源和审批状态。
- `project/`：读取 ProjectManifest，锁定 baseline id/version，并解析项目级 MC/DC、ProjectRulePack 和 Build/WinAMS metadata，形成 `ResolvedProjectContext`。
- `generation/`：按 `Obligation → Constraint → Solve → Evaluate → Oracle → Validate → Suite` 生成语义 Suite。它不依赖 learning、targets、WinAMS 或 Golden。
- `targets/winams/`：把 `SemanticTestSuite` 投影为 WinAMS TestCsv、Stub、DefineVar 和 Harness，并执行目标格式校验。
- `learning/`、`reporting/`：离线读取 Golden、语料和构建产物，生成语义比较、gap 和证据报告。

Target Projection 是 generation package 之外的独立领域，不是 generation 的额外阶段。正常生成不读取历史 Golden 补算 Oracle；无法证明的事实必须保留为 `UNKNOWN`、`UNSUPPORTED` 或 `NEEDS_REVIEW`。

## 配置组合

```text
ProjectManifest
  → TestBaseline @ version
  → project-level mcdc_enabled
  → optional approved ProjectRulePack / exceptions
  → ResolvedProjectContext
```

基础基准 identity 只表示基础测试规则，例如 `psd-rebuild@1.0`。MC/DC 是否启用由项目 manifest 决定。项目名和函数名不得成为通用 semantic patch 的分支条件。
