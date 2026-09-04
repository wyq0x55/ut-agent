# ut-agent 工程约束

始终称呼用户为 wan37。本文件只记录当前有效的工程约束；历史设计、阶段报告和已关闭 issue 的方案不构成当前实现要求。

## 当前事实源与依赖方向

唯一允许产生 C 源码语义事实的组件是 `tooling/ut-clang-extract`（C++/LibTooling）。它输出版本化的 Typed FunctionIR v3。Python 生产代码只能消费 FunctionIR，禁止使用 `clang.cindex`、正则表达式或源码文本重新推导 C 的类型、控制流、数据流和嵌套关系。

当前项目上下文模型为：

```text
ProjectManifest
  + versioned TestBaseline
  + project-level MC/DC switch
  + optional approved ProjectRulePack / exceptions
        ↓
ResolvedProjectContext
```

基础测试基准只表达基础测试规则和版本。MC/DC 是项目级开关，不编码进基础基准 identity；项目基准版本只由 ProjectManifest 锁定。不得为尚未存在的第二种独立可叠加需求增加通用策略层。

依赖方向必须保持：

```text
C++ extractor → Typed FunctionIR → project context → generation
                                             → targets/winams projection
```

`generation/` 与 `targets/winams/` 物理分离。generation 不依赖 learning、targets、WinAMS 适配器、网络、时间、随机数或模型调用；learning 只服务离线语料/Golden 分析，target package 只负责投影和执行适配。

## 生成与证据边界

- 正常生成只使用 FunctionIR、ResolvedProjectContext 和已审批规则；不得读取历史 Golden 来补算 Oracle 或制造输入值。
- 生成阶段顺序为 `Obligation → Constraint → Solve → Evaluate → Oracle → Validate → Suite`；`SemanticTestSuite → targets/winams` 是独立的目标投影。
- `UNKNOWN`、`UNSUPPORTED`、`NEEDS_REVIEW` 和缺失证据必须保持原状，不能用默认值、占位 CSV 或猜测伪装成 `VALIDATED`。
- Golden、历史 TestCsv 和构建产物都是学习/校验证据，不是正常生成的语义事实源。
- 每个真实 gap 必须在最上游真实 owner/root-cause 层修复，并同时添加最小 synthetic fixture 与真实函数回归；不得用项目名/函数名硬编码语义特例。
- 规则包必须有明确审批状态、版本和证据；候选规则不能进入正式生成。

## 配置、文档与语料

- `config/baselines/` 是正式 runtime TestBaseline；`config/projects/*.json` 是项目策略和基础基准版本的唯一绑定入口；`config/project-rules/` 保存项目规则包；不得新增第二套项目基准 registry。
- `docs/baselines/` 是来源证据和人工复核输入，不会因转录而自动变成 approved runtime rule。
- `docs/archive/**` 只用于理解历史资料，不是当前需求事实源，也不是 Agent 约束来源。
- `examples/` 是本地/外部、gitignored 的真实 corpus 挂载约定；不能假设 clone 后存在，也不能为了让文档或测试成立提交客户源码。可复用的 synthetic fixture 放在正式测试目录。
- README 只做入口；当前架构、项目配置、语料校验和 WinAMS contract 分别以 `docs/architecture.md`、`docs/project-config.md`、`docs/corpus-validation.md` 和 `docs/winams/coverage-csv.md` 为准。

## 临时产物与验证

- 新生成的 CSV、Golden 对比、日志、解压目录和报告统一放在 `.tmp/`；pytest 临时目录统一放在 `.pytest/`，例如 `--basetemp=.pytest/<run-id> -p no:cacheprovider`。
- 任务结束前检查 `git status` 和目录树，只清理本轮创建且不再需要的临时文件，不触碰来源不明的用户资料或参考 corpus。
- 可复用的检查器和转换工具放在 `script/`；脚本必须说明输入、输出和清理边界，并保持确定性。
- 运行 Python 测试前确认工作区临时目录位于仓库内；生成验证不等于 WinAMS GUI/执行完成，执行结论必须同时有进程状态和输出 artifact 证据。

## 工具偏好

- Shell 优先使用 pwsh。
- 文件发现使用 `fd`，文本搜索使用 `rg`/`git grep`，JSON 使用 `jq`，压缩包使用 `7z`；避免把一次性实验命令散落成仓库文件。
- Office/PDF/WinAMS 工作遵循对应技能和现有 artifact workflow；读写边界、编码、换行和 provenance 必须可复核。
