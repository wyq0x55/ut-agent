# 完整 Clang 解析层引入方案

> 目标：以 C++ LibTooling/ASTMatchers 建立唯一、可追溯、确定性的 C 语义事实源，
> 取代 Python `clang.cindex` 对 `FunctionIR` 的生产解析职责。Python 保留编排、
> 规则、用例、Stub、WinAMS 渲染与验证职责。

## 1. 决策与边界

### 决策

引入一个随仓库构建、版本锁定的独立程序 `ut-clang-extract`：

```text
C 源码 + CompileContext
        |
        v
ut-clang-extract (C++ / LibTooling)
        |
        +-- stdout: 一个 schema_version 固定的 FunctionIR JSON 文档
        +-- stderr: 诊断和运行日志
        v
Python 规则 / cases / stub / winams / host
```

生产路径只能消费该程序的成功 JSON；**禁止**在生产路径上出现“LibTooling 失败后
静默回退到 Python token/正则解析”的分支。无法构建语义事实时，必须显式失败并
产出结构化诊断。

### 本次不做

- 不把 WinAMS CSV 格式、`ARG<k>_`、`PTIN<k>_`、`PTOUT<k>_`、`CALLRET<k>`
  的命名语义移入 C++ 工具。
- 不在 C++ 工具中生成用例、学习规则、调用 LLM、运行 WinAMS 或访问网络。
- 不以 ASTMatchers 代替全部分析：它仅用于选择/绑定 AST 节点；CFG、数据流、
  宏来源、类型恢复由明确的 C++ pass 完成。
- 不长期维持两套 parser 的生产开关。旧 parser 只在迁移期间作为测试对照器，
  切换完成后删除或冻结到迁移测试工具中。

### 不可破坏的约束

1. 同一 `source bytes + CompileContext + extractor version` 必须生成字节稳定的 JSON。
2. 完整 Clang 只读取项目输入；不得引入 LLM、网络、时间、随机数。
3. 每项可影响用例生成的语义事实必须含来源证据；不可证明时输出
   `UNSUPPORTED`/`NEEDS_REVIEW`，不能猜测。
4. Python 后续流程只使用 schema 中的语义字段，不再从源码文本二次推断宏或条件。

## 2. 为什么必须升级

Python `libclang` 是稳定但有意保持较小的 C 接口；它不会提供完整 C++ AST 内部
信息。当前 parser 中已有 `UNEXPOSED_EXPR`、宏展开 extent/token 丢失、文本回退
和宏定义二次解析等补偿逻辑。这些补偿适合作为短期兼容，不能作为学习语料的
事实来源。

完整 C++ API 的目标不是“解析更多节点”，而是让每项事实都有确定来源：

- AST 原节点、精确类型与常量求值结果；
- `SourceManager` 的 spelling location 与 expansion location；
- `PPCallbacks` 采集的宏定义、宏调用与嵌套展开；
- `CFG` 与父子关系得到的真实控制结构；
- 固定的编译上下文，避免同一源码在不同目标/宏下得到不同 IR。

官方参考：

- <https://clang.llvm.org/docs/Tooling.html>
- <https://clang.llvm.org/docs/LibTooling.html>
- <https://clang.llvm.org/docs/LibASTMatchersTutorial.html>
- <https://clang.llvm.org/docs/LibClang.html>

## 3. 目标目录和构建边界

新增以下独立目录，不把 C++ 代码塞入 `src/ut_agent/`：

```text
tooling/
  CMakeLists.txt
  cmake/
  ut-clang-extract/
    CMakeLists.txt
    main.cpp
    compile_context.{h,cpp}
    extractor_action.{h,cpp}
    matcher_collector.{h,cpp}
    preprocessor_trace.{h,cpp}
    semantic_passes.{h,cpp}
    json_writer.{h,cpp}
    diagnostics.{h,cpp}
    tests/
      CMakeLists.txt
      fixtures/
      expected/
```

构建要求：

- 选定并锁定一个 LLVM/Clang 发行大版本；`clang`、resource headers、Clang C++
  libraries 必须来自同一安装前缀。
- CMake 使用该前缀的 `LLVMConfig.cmake`、`ClangConfig.cmake` 查找库；不能把
  Python wheel 内的 `libclang.dll` 当作 LibTooling SDK。
- 将工具链版本（LLVM、Clang、extractor schema）写入 `--version` 和输出 JSON。
- Windows 交付物采用明确的构建镜像或固定工具链包；不得依赖开发者机器 PATH 中
  恰好存在的另一套 LLVM。
- 首次开发先探测本机已有 Clang/CMake/SDK。若必须安装或下载 LLVM，应先请求用户
  授权，并记录选择的版本与来源。

`ut-clang-extract` 为独立可执行文件而不是 Clang plugin：它必须能按函数、文件或
项目子集独立运行，不应依赖客户工程的编译步骤触发。

## 4. 输入契约：CompileContext

当前 `-I/-D/-include` 是正确起点，但交付级解析还必须显式记录下列项。Python
创建 JSON 文件传给 extractor，禁止拼接未转义的 shell 参数。

```json
{
  "schema_version": 1,
  "language": "c",
  "standard": "c99",
  "source_files": ["absolute/canonical/path/to/source.c"],
  "include_dirs": ["absolute/canonical/include"],
  "defines": {"MACRO": "1"},
  "force_includes": ["absolute/canonical/config.h"],
  "target_triple": "...",
  "cpu": "...",
  "abi": "...",
  "sysroot": "...",
  "resource_dir": "...",
  "extra_args": []
}
```

实现规则：

1. `compile_commands.json` 存在时可转换为该对象；不能让其成为唯一输入模式。
2. 不存在编译数据库时，沿用 CLI 的 `-I/-D/-include`，并要求调用方补齐 target
   相关参数。
3. 所有路径在 Python 端和 C++ 端各规范化一次；输出保留可审查的 canonical path。
4. 传入的宏、强制包含文件、target 参数、resource dir 均进入输出的
   `compile_context`，成为证据的一部分。
5. 诊断达到 error/fatal 时，默认失败；仅允许已经定义的“无关函数错误隔离”策略，
   且其被忽略范围和原因必须写入 JSON。

## 5. 输出契约：版本化 FunctionIR JSON

先在 `docs/` 中写 JSON Schema，再同时由 C++ writer 和 Python dataclass 测试约束。
禁止让 Python 通过 `dict.get()` 静默吞掉 schema 字段缺失。

### 顶层结构

```json
{
  "schema_version": 3,
  "extractor": {"name": "ut-clang-extract", "version": "...", "clang_version": "..."},
  "status": "OK | PARTIAL | UNSUPPORTED | ERROR",
  "compile_context": {},
  "diagnostics": [],
  "functions": []
}
```

每个函数、分支、原子条件、调用点、全局/寄存器访问必须携带：

```json
{
  "provenance": {
    "spelling": {"file": "...", "line": 0, "column": 0, "offset": 0, "end_offset": 0},
    "expansion": {"file": "...", "line": 0, "column": 0, "offset": 0, "end_offset": 0},
    "macro_stack": ["OUTER_MACRO", "INNER_MACRO"],
    "ast_kind": "IfStmt"
  }
}
```

规定：

- `cond_text_spelling` 与 `cond_text_expanded` 分开；不把它们互相覆盖。
- `type_spelling`、canonical type、`QualType` 所含 const/volatile/指针层级分开保存。
- `branch_id` 由 canonical spelling path、offset、AST kind、链内位置按固定算法产生；
  不使用内存地址或遍历偶然顺序。
- 任何无法降为支持的比较/边界/指针语义都以结构化 issue 表示，不伪造为标量。
- JSON 数组使用源位置和稳定次序排序；JSON writer 固定 key 顺序、UTF-8、LF、无
  时间戳。这样 golden 才能进行字节级回归。

现有 Python `FunctionIR` 需要扩展 provenance 和显式状态字段；消费者尚未支持的
字段必须保留，不得在 adapter 中丢弃。

## 6. C++ 提取器内部设计

### 6.1 FrontendAction 与预处理记录

每个翻译单元创建一个 `FrontendAction`，在 `CreateASTConsumer` 时注册
`PPCallbacks`。`PreprocessorTrace` 至少记录：

- object-like/function-like 宏定义与 token 形式；
- 每次 `MacroExpands` 的调用点、定义点、参数 token、父宏栈；
- 条件编译分支及其是否被选中；
- include 链和不可用头文件诊断。

宏记录必须由 `SourceManager` 关联到 AST node；不能以“行号相等”猜测某个 if/call
来自哪个宏。

### 6.2 ASTMatchers：只负责粗粒度发现

用 matcher 收集并绑定下列节点：

- `functionDecl(isDefinition())`：函数边界、参数、返回类型；
- `ifStmt`、`forStmt`、`whileStmt`、`doStmt`、`switchStmt`、
  `conditionalOperator`：控制节点；
- `callExpr`：直接调用、间接/函数指针调用、实参、被调声明；
- `declRefExpr`、`memberExpr`、`arraySubscriptExpr`：变量/成员/数组引用候选；
- `binaryOperator`、`unaryOperator`：条件和赋值候选；
- `varDecl(hasGlobalStorage())`、`enumDecl`：全局变量与枚举事实。

matcher callback 只采集 node handle 与源位置，交给后续 pass；不要在 callback 中混入
WinAMS 列名、用例组合或文本正则。

### 6.3 语义 pass

按下列顺序实现，每一个 pass 只写自己的字段并产生可审查 issue：

1. **DeclarationPass**：函数、参数、全局、枚举、typedef、record 的精确类型。
2. **ControlPass**：从真实 `Stmt` 构建分支树、else-if 链、switch case/default，生成
   稳定 branch id。
3. **ConditionPass**：结构化拆解 `&&`/`||`/`!`/比较/位掩码；使用 Clang 常量求值和
   原表达式，而非重新 tokenize 源码。
4. **CallPass**：解析 `CallExpr`、callee declaration、函数指针/表项、参数类型、
   返回类型和宏来源。
5. **ReadWritePass**：基于 AST/CFG 收集参数指向物、全局和 memory-mapped 访问的
   读写事实；未知别名必须标注，不能假定无写入。
6. **ConfigurationPass**：只传播已在 `CompileContext` 和 AST 中可证明的常量；恒真/
   恒假结论必须保留证明表达式与来源。
7. **ValidationPass**：schema 完整性、位置单调性、引用目标存在性、无重复 ID、
   输出排序；失败不能写 `OK`。

### 6.4 CFG 的使用边界

`CFG` 用于控制可达、赋值路径和精确写入分析。它不能在第一阶段被夸大为全程序
定理证明器：跨函数、volatile、inline asm、未知函数指针、硬件别名等必须明确
输出 `UNSUPPORTED` 或 `NEEDS_REVIEW`，交给既有验证闭环处理。

## 7. 分阶段实施与验收

### P0：冻结契约和证据集

**只做设计与测试，不迁移生产 parser。**

- 写 `CompileContext` 和 FunctionIR JSON Schema v3。
- 为现有 Python IR 写从 JSON 到 dataclass 的严格 adapter；未知 schema 版本、缺少
  必填字段、非法状态必须失败。
- 新增最小、脱敏的 C fixtures，至少覆盖：配置头、对象宏、函数宏包裹条件、位掩码、
  switch/default、else-if、指针输出、函数指针、volatile 寄存器、不可解析输入。
- 将现有 SetPduMode/DMA golden 中可公开的语义期望整理成“事实断言”，而不是只比
  最终 CSV。

**验收**：证据集可在无 WinAMS 环境运行；每条期望都有来源/理由；schema 评审完成。

### P1：工具骨架和可重复构建

- 新建 `tooling/` CMake 工程，锁定 LLVM/Clang 版本和最小依赖。
- 实现 `--version`、`--context <file>`、`--output <file>`、`--function <name>`。
- 实现 FrontendAction、诊断序列化、顶层 JSON、`CompileContext` 回显。
- 先只实现函数声明、参数、返回类型、精确 source range。

**验收**：同一 fixture 连续运行两次 `cmp` 输出完全相同；错误上下文不会产生伪
成功 JSON；Python adapter 能读取成功结果。

### P2：宏、控制结构、调用和条件

- 完成 PPCallbacks/SourceManager provenance。
- 完成 ControlPass、ConditionPass、CallPass，覆盖 P0 所有 fixtures。
- 对每个条件同时检查 spelling/expanded 文本、边界/枚举值、宏栈、stable id。

**验收**：此前依赖 token/正则回退的真实回归用例均由 AST/provenance 直接得到；
出现不支持结构时得到确定的 `UNSUPPORTED`，绝不静默降级。

### P3：读写事实和 Python 流水线接入

- 完成 ReadWritePass/ConfigurationPass，映射现有 `ControlVar`、`MemoryVar`、
  `CallSite` 所需事实。
- Python 新增 extractor client：只做 context 文件、进程调用、JSON schema 校验与
  dataclass 映射；不重新解析 C。
- `batch`/`gen` 在测试环境改为使用新 extractor；WinAMS renderer 的格式逻辑保持不变。

**验收**：SetPduMode golden 回归、`tests/test_setpdumode_golden.py`、WinAMS contract
测试和 rules engine 测试通过；真实函数的生成 TestCsv 与人工 golden 做语义对比，
不要求 CSV 行号或排版字节相同。

### P4：交付校准、切换与移除旧路径

- 对完整 BSW corpus 批处理，输出按函数的支持状态、解析 issue、旧/新语义差异和
  WinAMS/host 验证结果。
- 人工审查每个差异：新工具修正、旧工具缺陷、fixture 期望错误、或显式不支持。
- 一旦证据集和指定交付模块达到目标，生产入口只调用新 extractor。
- 删除旧 parser 的生产调用与 token/正则 fallback；保留有限的迁移 fixture，避免
  两个事实源长期漂移。

**验收门槛**：交付范围内不存在未分类的语义差异；所有未支持项显式列入报告；
WinAMS 执行和覆盖率满足已批准的基线；生成物、IR、诊断均可追溯至工具版本与
CompileContext。

## 8. 测试、差异审查和证据包

测试分为三类，缺一不可：

| 层级 | 输入 | 断言 |
|---|---|---|
| C++ unit | 小 fixture + context | AST/宏/类型/CFG 事实、JSON 字节稳定 |
| Python integration | extractor JSON | schema 严格解析、FunctionIR 映射、无二次文本推断 |
| 端到端 | 真实函数/批准语料 | TestIntent 语义、CSV 契约、host/WinAMS 结果 |

每次解析运行保存最小证据包：source digest、CompileContext、extractor/Clang 版本、
JSON、diagnostics、输入/输出 SHA-256。不要把商用 WinAMS 项目或未脱敏客户源码
直接提交仓库。

差异报告按以下分类，不允许写“不同但原因不明”：

- `NEW_CORRECTNESS_FIX`：新工具有证据表明修正旧解析；
- `LEGACY_EXPECTATION`：旧 golden 的语义期望错误；
- `SCHEMA_GAP`：新 IR 尚无字段表达；
- `UNSUPPORTED`：已知且被明确拒绝的语言/工程结构；
- `REGRESSION`：新工具缺失或错误，必须修复，不得通过降级掩盖。

## 9. Luna 的执行纪律

Luna 应严格按 P0 → P1 → P2 → P3 → P4 推进；一次只推进一个阶段，阶段验收
失败时先修复，不跳到后续 WinAMS 或学习功能。

- 首先阅读 `AGENTS.md`、本方案、`src/ut_agent/ir.py`、现有 parser 的 golden 测试。
- 先检查 Git 状态，保留用户已有的未提交改动；不要重置、覆盖或格式化无关文件。
- 在 P0/P1 前不修改 `cases/`、`stub/`、`winams/` 的语义，更不修改 CSV 命名规格。
- 不要在 C++ 中调用网络、LLM、随机数或当前时间；不要为“方便通过”加入 regex/token
  fallback。
- 每次变更后运行与变更比例相当的测试；解析器变更必须运行
  `tests/test_setpdumode_golden.py`。
- 发现环境缺少 LLVM/Clang 开发 SDK 时，先报告探测结果和精确安装需求；不要擅自
  下载大工具链或切换系统编译器。
- 任何 `UNSUPPORTED` 都必须带结构化 reason、provenance 和最小复现 fixture。

### 可直接发送给 Luna 的首条任务

```text
请在 C:\\workspeace\\ut-agent 严格执行 docs/完整Clang解析层引入方案.md 的 P0，
不要开始 P1 或安装 LLVM。先阅读 AGENTS.md 和方案，再检查工作区已有改动并保持
它们不受影响。为完整 Clang extractor 定义 CompileContext 与 FunctionIR JSON Schema v3，
实现 Python 端的严格 schema adapter 测试，并建立最小脱敏 C fixture 证据集。不要
修改 WinAMS CSV 规格、cases/stub/winams 的业务逻辑，也不要使用 token/regex fallback。
完成后运行相关 pytest（包括 tests/test_setpdumode_golden.py），报告改动、测试结果、
尚需用户决定的 LLVM/Clang 工具链选型；不要自行进入下一阶段。
```

## 10. 用户需要决定的事项

在 P1 前由用户批准：

1. 锁定的 LLVM/Clang 发行版本与分发方式（Windows 原生包、WSL 镜像或受控 CI 镜像）。
2. 交付第一批涵盖哪些 target/ABI（host、ARM、RH850 等）；每个 target 都需要可复现
   的 CompileContext 和系统头策略。
3. 交付工程是否允许提交脱敏 C fixtures；若不允许，提供可在本机运行的外置证据集
   路径和摘要清单。
