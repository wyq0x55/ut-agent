# 单元测试 AI Agent 实施计划

> 依据《单元测试自动化方案.md》与 Issues #1 ~ #6 落地：基于 `ut-clang-extract` C++ Extractor 的确定性单元测试生成流水线，90% 规则确定性生成 + 3 个 LLM 介入点，全部带机器校验闭环。

---

## 0. 定位与范围

- **目标**：把方案文档变成可运行、可复现、可评测的系统，最终能对真实项目模块批量产出达标的 C0/C1/MC-DC 测试与审查报告。
- **范围**：以 WinAMS 单元测试流水线为主；采用 C++ LibTooling Extractor 作为唯一 C 源码语义事实源。
- **不变的铁律**（写进 AGENTS.md）：
  1. 确定性核心路径永不引入 LLM（同输入必须同输出，这是审查一致性的根基）
  2. 单一事实源：`ut-clang-extract`（C++ LibTooling）是唯一 C 源码语义分析引擎，Python 不重新解析 C 源码
  3. LLM 一切输出必须有机器校验闭环（编译通过 / 覆盖率达标 / schema 合法）
  4. 所有 prompt 版本化、用量记账，token 成本按函数可核算

---

## 1. 仓库物理结构与技术选型

```
src/ut_agent/
├── ir/            # C++ extractor 产出的 Typed FunctionIR (v3) schema, dataclass 与 codec
├── baseline/      # 版本化、已审批 Base TestBaseline 与 Requirement Modules 加载与校验
├── project/       # 项目 Manifest、Registry、ResolvedTestPolicy 解析与锁定
├── generation/    # 确定性生成引擎 (Obligation → Constraint → Solve → Evaluate → Oracle → Validate → Suite)
├── targets/winams/# CSV、Harness、DefineVar、Stub 和 WinAMS 投影适配器
├── learning/      # Golden 语义标准化、语义 Diff、Gap 分类与规则推导（仅对比/学习，不介入生成）
├── reporting/     # 语料库差异报告与证据链生成
├── toolchain/     # 唯一 C++ Clang Extractor 驱动、CompileContext 构建与进程边界
├── cli/           # 统一 CLI 解析与命令路由
└── llm/           # 三个介入点（仅非确定性辅助路径）
```

**选型**：

| 项 | 选择 | 理由 |
|---|---|---|
| 主控 | 裸 Python 3.10+ | 线性流水线，逻辑严密，无框架开销 |
| 解析 | `ut-clang-extract`（C++ LibTooling） | 唯一 C 语义事实源，保留 typed AST 与 provenance |
| 规则引擎 | 8 阶段确定性生成引擎 | 高效、100% 确定性、同输入同输出 |
| 目标投影 | WinAMS Adapter (CP932/CRLF CSV, Harness, DefineVar) | 隔离生成语义与目标工具格式 |
| LLM | OpenAI 兼容接口，模型可配置 | 仅在来源判定兜底、有状态 stub、覆盖率闭环介入 |

---

## 2. 里程碑与发展方向

### Phase 1: 锁存单一 C 源码事实源 (Issue #1)
- C++ Extractor 输出 Typed `FunctionIR` v3 JSON/Dataclass。
- 清理 Python 内部一切 C 源码正则与补推断逻辑，添加 `check_architecture.py` CI 门禁。

### Phase 2: 确定性生成引擎重构 (Issue #2)
- 实现从 `ResolvedTestPolicy` + `FunctionIR` 到 `Obligation` -> `Constraint` -> `Solve` -> `Evaluate` -> `Oracle` -> `Validate` -> `Suite` 的完整 8 阶段流水线。
- `SemanticEvaluator` 基于 UUT 源码语义求值 post-state，Oracle 提取 Caller-visible expected effects，不读取 Golden。

### Phase 3: 领域边界与包结构重构 (Issue #3)
- 完成 `ir`, `baseline`, `project`, `generation`, `targets/winams`, `learning`, `reporting`, `toolchain` 的物理分离。

### Phase 4: 组合式策略模型 (Issue #5)
- 建立 `ResolvedTestPolicy = Base TestBaseline + Requirement Modules + ProjectRulePack` 机制。

### Phase 5: 首个真实项目闭环与 Gap 分类 (Issue #6)
- 以 `N-O2608-PSD-087` 真实项目（PSD再構築 + MC-DC）建立全项目语义差异驱动迭代机制。
- 引入标准 Gap Taxonomy，所有差异严格分类并在最上游根因层修复，同步补齐测试。
