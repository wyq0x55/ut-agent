# AGENTS.md — ut-agent 项目约束与架构准则


本项目由编码 agent 参与开发，以下为硬约束：

## 铁律（不可违反）

1. **确定性核心**：`ir/ baseline/ project/ generation/ targets/ toolchain/` 为确定性生成主链路，禁止引入 LLM 调用、网络访问、随机数、时间依赖。同输入必须同输出（这是审查一致性的根基）。
2. **LLM 只在三个介入点**：来源判定兜底、有状态 stub、覆盖率闭环（均在 `llm/` 内，且必须有机器校验闭环）。
3. **命名遵循规格**：`docs/用例表与CSV格式规格.md` 的命名与语义不得擅改；规格变更以该文档 §7 拍板记录为准。
4. **称呼**: 始终称呼我为wan37，用以判断agent是否丢弃AGENTS.md。
5. **工程**：优先给出满足目标的最小完整方案，而不是补丁式兼容方案；但如果“最短路径”与“非补丁”冲突，应优先选择不会引入结构性错误的最小正确方案。不做与当前需求无关的兜底、降级或额外分支设计；但为保证逻辑闭合，允许加入必要的输入约束、状态检查和边界保护。

---

# Tool Preference

- Git Bash for shell execution
- fd for file discovery
- rg for text search
- git grep when appropriate
- choco for software installation
- jq for JSON processing
- bat for file viewing
- ast-grep for code-aware searching
- 7z for extract compressed files

Avoid:
- PowerShell Get-ChildItem when fd is available
- PowerShell Select-String when rg is available
- Manual software download when choco can install it

---

## 测试用例与临时产物管理

1. `examples/` 是可保留、可复用和作为对照基线的测试用例目录。用户提供的旧版 WinAMS `TestCsv`、golden 以及已经确认要保留的生成用例都放在这里，并按项目/函数组织；不要在仓库根目录或其他目录重复创建测试用例副本。
2. 新生成但尚未确认的 `TestCsv`、对比结果、日志、解压目录、单次报告和其他临时文件统一放在 `.tmp/`。临时文件不得散落到 `examples/`、仓库根目录或源码目录。只有用户明确需要保留或已通过验收的结果，才能从 `.tmp/` 晋级到 `examples/`。
3. 所有 pytest 临时目录和缓存统一放在 `.pytest/`，例如使用项目内的 `--basetemp=.pytest/<run-id>`；禁止在根目录生成或累积 `.pytest-tmp-*`、`pytest-of-*`、`.pytest_cache` 等同类目录。每轮测试结束后清理本轮目录，并定期删除 `.pytest/` 下已完成且不再需要的旧运行目录。
4. 每次生成、对比或测试阶段结束时，清理上一轮由 agent 创建的 `.tmp/` 和 `.pytest/` 临时产物；任务结束前检查 `git status` 和目录树，确保没有遗留的临时测试用例。只删除能确认由本轮 agent 生成且不再需要的文件，不删除用户放入 `examples/` 的参考资料或其他来源不明的文件。
5. 具有复用价值的命令、检查器和转换工具放在 `script/`，不要把可复用脚本留在 `.tmp/` 或以内联命令的形式反复散落在仓库中。脚本应保持确定性，并说明输入、输出及清理边界；一次性实验命令不需要落盘。

---

## 架构边界与单一事实源（Issues #1 ~ #6）

### 1. 单一 C 源码语义事实源 (Issue #1)
- `tooling/ut-clang-extract`（C++ / LibTooling）是仓库中**唯一**允许解析 C 源码、提取类型与控制流语义的分析引擎；它产生版本化的 Typed `FunctionIR` (v3)。
- `src/ut_agent` 中的 Python 生产代码**严禁**引入 `clang.cindex` 或通过正则表达式/源码文本重新推导 C 语义、类型拼写或控制流嵌套关系。
- 依赖方向严格限定为：
  `C++ Extractor -> Typed FunctionIR -> Resolved TestPolicy -> Generation Pipeline -> Target Projection`
- 若 Extractor 无法证明某项 C 源码事实，必须在 FunctionIR 中输出 `UNSUPPORTED`/`NEEDS_REVIEW`，严禁 Python 端做默示 fallback 或正则猜算。

### 2. 组合式测试策略模型 (Issue #5)
- 生成策略采用可组合模型：
  `ResolvedTestPolicy = Base TestBaseline + Requirement Modules (如 MC/DC) + ProjectRulePack`
- 基础测试基准（Base TestBaseline，如 PSD再構築 Ver.1.6）定义通用的测试点展开与边界生成规则。
- 附加需求模块（Requirement Modules，如 MC/DC）可灵活叠加到任意 Base Baseline 上。
- 项目附加规则包（ProjectRulePack）仅用于配置已审批的项目特定参数，严禁在 Python 通用代码中根据项目名/函数名硬编码 `if project_id == ...` 分支。

### 3. 确定性测试生成主链路 (Issue #2)
- 生成链路包含 8 个严格分工的确定性阶段：
  1. **Obligation**: 根据 `ResolvedTestPolicy` 与 `FunctionIR` 构造测试观点（C0/C1/MC/DC/边界/switch/entry 等）。
  2. **Constraint**: 将 Obligation 转换为类型化的域约束。
  3. **Solve**: 确定性求解满足 constraint 的输入组合 witness（输入参数、全局变量前置状态、Stub 返回值/指针输出）。
  4. **Evaluate**: 使用 `SemanticEvaluator` 根据 C 源码语义计算内部与后置状态演变。
  5. **Oracle**: 从 post-state 提取 Caller-visible 的预期效果（返回值、全局变量后置状态、Stub 捕获与输出）。
  6. **Validate**: 校验输入域合法性、分支执行情况与 Oracle 完整性。
  7. **Suite**: 组合、排序与去重测试用例，保持独立语义观点。
  8. **Target Projection**: 将语义 Suite 投影为具体目标工具产物（如 WinAMS TestCsv / Harness / DefineVar）。
- **铁律**：正常生成（`ut-agent gen`）**绝对禁止**读取历史 Golden TestCsv / 外部预期结果补算 Oracle。所有 Expected Effects 必须通过 UUT 源码语义求值得到。

### 4. 物理领域隔离与包结构 (Issue #3)
物理包结构划分为清晰的领域职责：
- `ir/`: Typed FunctionIR dataclass 映射、Schema 校验与 JSON 编解码。
- `baseline/`: Base TestBaseline 与 Requirement Modules 加载、解析与校验。
- `project/`: Project Manifest, Registry 以及 ResolvedTestPolicy 解析。
- `generation/`: 确定性生成引擎（obligation, constraint, solver, evaluator, oracle, pack, suite, validation）。
- `targets/winams/`: WinAMS 目标投影、Harness、CSV 渲染器与 DefineVar 生成。
- `learning/`: Golden 语义标准化、语义 Diff、Gap 分类与规则推导（仅用于学习/校验/对比命令，严禁被 `gen` 依赖）。
- `reporting/`: 语料库差异报告、证据链生成。
- `toolchain/`: C++ Extractor 驱动、CompileContext 构建与进程管理。

### 5. Gap 统一分类法与语料库闭环 (Issue #6)
- 所有生成用例与 Golden 差异必须被归类到标准 Gap 分类法：
  - `BASELINE_GAP`: 审核基准包含该要求但结构化 Baseline 未表达。
  - `PROJECT_RULE_GAP`: 项目特有且已审批的规则缺口。
  - `FUNCTION_IR_GAP`: Extractor 缺失必要的 C 源码 typed 事实。
  - `OBLIGATION_GAP`: Baseline 已定义但 ObligationBuilder 未展开。
  - `SOLVER_GAP`: Constraint 存在但无法求解或 witness 代表值不佳。
  - `EVALUATOR_GAP`: Evaluator 状态转移计算不准确。
  - `ORACLE_GAP`: Post-state 完整但 OracleBuilder 漏选 expected effect。
  - `SUITE_GAP`: Suite 合并/排序导致独立观点丢失。
  - `HARNESS_GAP`: Harness 存根/指针/内存绑定问题。
  - `PROJECTION_GAP`: 仅格式/渲染/编码不同。
  - `GOLDEN_ERROR`: Golden 存在确认的错误（需提交证据）。
- 严禁掩盖 Gap 或打特例补丁。所有 Gap 修复必须在最上游真实 Root Cause 层进行，并同时包含**最小 Synthetic Fixture 测试**与**真实函数回归测试**。
