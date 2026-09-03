# ut-agent

嵌入式 C 单元测试用例自动生成流水线。以基于 C++ LibTooling 提取的 Typed FunctionIR 为单一 C 源码事实源，采用确定性测试基准驱动的规则引擎，生成满足 C0/C1/MC-DC 与边界条件的测试用例。LLM 仅在三个闭环介入点兜底。

## 架构概览

```
                C Source + CompileContext
                            │
                            ▼
           ut-clang-extract (C++ LibTooling)
                            │
                            ▼
          Typed FunctionIR v3 (JSON / Dataclass)
                            │
                            ▼
    ResolvedTestPolicy (Base Baseline + Requirements + ProjectRules)
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│              确定性测试生成引擎 (generation/)             │
├─────────────────────────────────────────────────────────┤
│ 1. Obligation  │ 根据策略与 IR 构造测试观点 (C0/C1/MC-DC)   │
│ 2. Constraint  │ 将观点转换为类型化域约束                   │
│ 3. Solve       │ 确定性求解输入组合与 Witness                │
│ 4. Evaluate    │ 基于 C 源码语义计算 post-state (Evaluator)  │
│ 5. Oracle      │ 提取 Caller-visible 期望效果 (Expected Effects) │
│ 6. Validate    │ 校验输入合法性与 Oracle 完整性               │
│ 7. Suite       │ 组合、去重与排序测试用例                     │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
                 WinAMS Projection (targets/winams/)
                            │
                            ▼
              TestCsv / Harness / DefineVar / Stub
```

### 核心物理领域划分 (`src/ut_agent/`)

- `ir/`: Typed FunctionIR dataclass 映射、Schema 校验与 JSON 编解码。
- `baseline/`: 基础测试基准（如 `psd-rebuild-v1.6`）与需求模块（如 `mcdc`）加载器与校验器。
- `project/`: 项目 Manifest、注册表与 `ResolvedTestPolicy` 策略解析器。
- `generation/`: 确定性生成引擎主链路（8 个阶段：Obligation, Constraint, Solve, Evaluate, Oracle, Validate, Suite, Target Projection）。
- `targets/winams/`: WinAMS 目标投影、Harness、CSV 渲染器与 DefineVar 生成。
- `learning/`: Golden 语义标准化、语义 Diff、Gap 分类与规则推导（仅用于对比/校验/学习命令，`gen` 严禁依赖）。
- `reporting/`: 语料库差异报告与证据链生成。
- `toolchain/`: C++ Extractor 驱动与 CompileContext 管理。
- `cli/`: 统一命令行入口。

---

## 策略与规则模型 (Issues #2 & #5)

测试策略采用三层可组合模型：
```
ResolvedTestPolicy = Base TestBaseline + Requirement Modules + ProjectRulePack
```
1. **Base TestBaseline**: 基础测试基准（如 `psd-rebuild-v1.6`），定义测试点展开、边界生成与 Oracle 提取规则。
2. **Requirement Modules**: 可叠加的功能需求模块（如 `psd-rebuild-mcdc/1.0`），可跨项目灵活组合。
3. **ProjectRulePack**: 经过审批的项目专属附加规则（如 `N-O2608-PSD-087` 规则包）。通用代码禁止针对特定项目硬编码分支。

---

## 语料库闭环与 Gap 分类法 (Issue #6)

为评估与闭环与历史 Golden TestCsv 的差异，`learning` 模块提供统一的差异分类法（Gap Taxonomy）：

- `BASELINE_GAP`: 审核基准包含但结构化 Baseline 未表达。
- `PROJECT_RULE_GAP`: 项目特有且已审批规则缺口。
- `FUNCTION_IR_GAP`: Extractor 缺失必要的 C 源码 typed 事实。
- `OBLIGATION_GAP`: Baseline 已定义但 ObligationBuilder 未展开。
- `SOLVER_GAP`: Constraint 存在但无法求解或 witness 代表值不佳。
- `EVALUATOR_GAP`: Evaluator 状态转移计算不准确。
- `ORACLE_GAP`: Post-state 完整但 OracleBuilder 漏选 expected effect。
- `SUITE_GAP`: Suite 合并/排序导致独立观点丢失。
- `HARNESS_GAP`: Harness 存根/指针/内存绑定问题。
- `PROJECTION_GAP`: 仅格式/渲染/编码不同。
- `GOLDEN_ERROR`: Golden 存在确认的错误。

**铁律**：正常生成（`ut-agent gen`）不依赖历史 Golden 补算答案，所有 Expected Effects 均由 `SemanticEvaluator` 根据 UUT 源码语义求值得到。

---

## 命令行使用

```bash
# 1. 提取 Typed FunctionIR
ut-agent extract --context compile-context.json --function target_func -o ir.json

# 2. 基于项目策略生成 WinAMS 产物
ut-agent gen --project N-O2608-PSD-087 --function target_func --out .tmp/output/

# 3. 语料库对比与差异分析（用于学习与闭环验证）
ut-agent compare --project N-O2608-PSD-087 --function target_func --golden path/to/golden.csv

# 4. 项目语料库批量校验与报告生成
ut-agent project validate --project N-O2608-PSD-087 --corpus-dir examples/N-O2608/
```

---

## 铁律（遵循 `AGENTS.md`）

1. **确定性核心**：`ir/ baseline/ project/ generation/ targets/ toolchain/` 为确定性生成主链路，禁止引入 LLM 调用、随机数或时间依赖。同输入必须同输出。
2. **单一事实源**：`tooling/ut-clang-extract` 是唯一 C 源码语义分析引擎，Python 端严禁引入 `clang.cindex` 或正则补推 C 语义。
3. **LLM 仅三个介入点**：来源判定兜底、有状态 stub、覆盖率闭环。
4. **Golden 是对比证据**：正常生成不读取 Golden，语义差异必须按 Gap 分类并在最上游根因层修复。

---

## 依赖与构建

- Python >= 3.10
- LLVM/Clang 16+（构建 C++ `tooling/ut-clang-extract`）
- Arm GNU Toolchain（`arm-none-eabi-gcc`，可选）

---

## License

MIT
