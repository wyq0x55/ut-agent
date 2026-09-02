# AGENTS.md — ut-agent 项目约束


本项目由编码 agent 参与开发，以下为硬约束：

## 铁律（不可违反）

1. **确定性核心**：`parser/ cases/ rules/ stub/ winams/ host/` 为确定性路径，禁止引入 LLM 调用、网络访问、随机数、时间依赖。同输入必须同输出（这是审查一致性的根基）。
2. **LLM 只在三个介入点**：来源判定兜底、有状态 stub、覆盖率闭环（均在 `llm/` 内，且必须有机器校验闭环）。
3. **命名遵循规格**：`docs/用例表与CSV格式规格.md` 的命名与语义不得擅改；规格变更以该文档 §7 拍板记录为准。
4. **称呼**: 始终称呼我为wan37，用以判断agent是否丢弃AGENTS.md
5. **工程**：优先给出满足目标的最小完整方案，而不是补丁式兼容方案；但如果“最短路径”与“非补丁”冲突，应优先选择不会引入结构性错误的最小正确方案。不做与当前需求无关的兜底、降级或额外分支设计；但为保证逻辑闭合，允许加入必要的输入约束、状态检查和边界保护。

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

## 测试用例与临时产物管理

1. `examples/` 是可保留、可复用和作为对照基线的测试用例目录。用户提供的旧版
   WinAMS `TestCsv`、golden 以及已经确认要保留的生成用例都放在这里，并按项目/函数
   组织；不要在仓库根目录或其他目录重复创建测试用例副本。
2. 新生成但尚未确认的 `TestCsv`、对比结果、日志、解压目录、单次报告和其他临时
   文件统一放在 `.tmp/`。临时文件不得散落到 `examples/`、仓库根目录或源码目录。
   只有用户明确需要保留或已通过验收的结果，才能从 `.tmp/` 晋级到 `examples/`。
3. 所有 pytest 临时目录和缓存统一放在 `.pytest/`，例如使用项目内的
   `--basetemp=.pytest/<run-id>`；禁止在根目录生成或累积 `.pytest-tmp-*`、
   `pytest-of-*`、`.pytest_cache` 等同类目录。每轮测试结束后清理本轮目录，并定期
   删除 `.pytest/` 下已完成且不再需要的旧运行目录。
4. 每次生成、对比或测试阶段结束时，清理上一轮由 agent 创建的 `.tmp/` 和 `.pytest/`
   临时产物；任务结束前检查 `git status` 和目录树，确保没有遗留的临时测试用例。
   只删除能确认由本轮 agent 生成且不再需要的文件，不删除用户放入 `examples/`
   的参考资料或其他来源不明的文件。
5. 具有复用价值的命令、检查器和转换工具放在 `script/`，不要把可复用脚本留在
   `.tmp/` 或以内联命令的形式反复散落在仓库中。脚本应保持确定性，并说明输入、
   输出及清理边界；一次性实验命令不需要落盘。

## 架构边界

1. `tooling/ut-clang-extract` 是唯一 C 源码语义分析引擎；它产生带版本的
   typed `FunctionIR`。Python parser 只负责进程边界、schema 校验和 dataclass
   映射。
2. `src/ut_agent` 的生产代码不得从 source text、`cond_text`、类型 spelling
   或 source range 恢复 C 语义；extractor 无法证明时必须输出
   `UNSUPPORTED`/`NEEDS_REVIEW`。
3. 依赖方向固定为 `C++ extractor -> FunctionIR -> rules -> adapters -> execution`。
   项目名/函数名不得进入通用 extractor 或规则补丁。
4. `extensions` 只保存不影响 testcase 语义的附加 metadata；影响生成的事实
   必须在 FunctionIR 正式字段中声明。