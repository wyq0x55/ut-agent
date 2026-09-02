# AGENTS.md — ut-agent 项目约束

## Issue #1 架构边界

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

本项目由编码 agent 参与开发，以下为硬约束：

## 铁律（不可违反）

1. **确定性核心**：`parser/ flow/ cases/ stub/ winams/ host/` 为确定性路径，禁止引入 LLM 调用、网络访问、随机数、时间依赖。同输入必须同输出（这是审查一致性的根基）。
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
