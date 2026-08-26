# AGENTS.md — ut-agent 项目约束

本项目由编码 agent 参与开发，以下为硬约束：

## 铁律（不可违反）

1. **确定性核心**：`parser/ flow/ cases/ stub/ winams/ host/` 为确定性路径，禁止引入 LLM 调用、网络访问、随机数、时间依赖。同输入必须同输出（这是审查一致性的根基）。
2. **LLM 只在三个介入点**：来源判定兜底、有状态 stub、覆盖率闭环（均在 `llm/` 内，且必须有机器校验闭环）。
3. **golden 是契约**：`examples/golden/` 与 `tests/` 下的 golden 期望值改动必须在提交说明中写明理由与拍板出处。
4. **命名遵循规格**：`docs/用例表与CSV格式规格.md` 的 ARG<k>_ / PTIN<k>_ / PTOUT<k>_ / CALLRET<k> / CALL_MAX / @地址 等命名与语义不得擅改；规格变更以该文档 §7 拍板记录为准。
5. **不引入重量级框架**（LangChain/LangGraph 等）。裸 Python + dataclass + 纯函数。

## 工程约定

- Python >= 3.10，类型注解，dataclass 建 IR，禁止全局可变状态。
- 解析层输入三件套：C 源码树 + include 路径 + **配置头/宏定义**（配置是一等输入，随项目而变）。
- 测试：pytest；解析器改动必须跑 `tests/test_setpdumode_golden.py` 回归。
- 中文注释与文档命名沿用现有风格。

# Tool Preference

- Git Bash for shell execution
- fd for file discovery
- rg for text search
- git grep when appropriate
- choco for software installation
- jq for JSON processing
- bat for file viewing
- ast-grep for code-aware searching

Avoid:

- PowerShell Get-ChildItem when fd is available
- PowerShell Select-String when rg is available
- Manual software download when choco can install it