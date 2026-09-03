> Historical document. Not normative. Current architecture: ../../architecture.md
# WinAMS 覆盖率测试 CSV 生成引擎 PRD

## 1. 产品概述

### 1.1 产品名称

WinAMS 覆盖率测试 CSV 生成引擎

### 1.2 产品目标

输入项目编号、C 源码、目标函数及编译配置，依据项目对应的测试观点和既有测试用例划分规则，自动生成符合 WinAMS 格式约束的：

- 覆盖率测试 CSV
- Stub 源码
- DefineVar.dat
- 生成诊断报告

### 1.3 产品定位

本产品是一个确定性生成引擎：

```text
项目测试观点映射
+ Clang AST 源码分析
+ 测试用例划分规则
+ 历史用例验证
+ WinAMS 格式输出
```

不负责自动判断业务需求是否正确，也不保证所有业务期望值都能从源码推导。

---

## 2. 核心原则

1. 相同输入必须生成相同结果。
2. 源码 AST 是程序语义的主要依据。
3. 项目测试观点决定使用哪套用例划分规则。
4. 历史用例用于验证和补充规则，不直接复制。
5. CSV 格式生成与测试用例划分解耦。
6. 正式生成主链路不依赖 LLM、网络、随机数或当前时间。

---

## 3. 输入与输出

### 3.1 输入

- 项目编号
- C/H 源码
- 目标函数名
- Include 路径
- 配置宏或配置头文件
- 测试观点映射配置
- 测试用例划分规则
- 可选历史源码、CSV 和 Stub 样本
- 可选 Soft.map、Soft.mot、Soft.out、Soft.out.xlo

### 3.2 输出

```text
<function>_testdata.csv
<function>_stubs.c
DefineVar.dat
generation_report.json
```

CSV 必须使用 CP932 编码和 CRLF 换行。

---

## 4. 核心流程

```text
项目编号
  ↓
解析测试观点 Profile
  ↓
Clang AST 分析目标函数
  ↓
生成 FunctionIR
  ↓
识别条件、调用、输入、输出和寄存器
  ↓
按 Profile 匹配用例划分规则
  ↓
生成抽象测试用例
  ↓
与历史用例进行规则一致性验证
  ↓
生成 WinAMS CSV、Stub 和 DefineVar.dat
  ↓
格式校验并输出诊断报告
```

---

## 5. 功能需求

### 5.1 项目测试观点解析

系统根据项目编号选择已审核的基础测试 Profile，并独立读取 MC/DC 维度。例如：

- PSD 再构筑
- MS-TAT-PGD-WEL
- PBD
- SRF
- FSC 半自动化
- MC/DC 作为可叠加维度，不是与 PSD/PBD/SRF 互斥的基础 Profile

项目 Profile 至少包含：

```text
project_id
base_profile
mcdc_enabled
approved_exceptions
profile_version
```

不得仅根据项目名称猜测分类。项目编号与基础 Profile 的映射必须来自已审核的
矩阵配置；无法匹配、出现多个冲突映射或缺少审批版本时，停止正式生成并输出诊断。

### 5.2 源码分析

使用 Clang AST 和预处理信息识别：

- 函数参数、返回值和类型
- 全局变量和数组
- `if`、`else if`、`switch` 和循环条件
- `&&`、`||` 和比较运算
- 类型转换和宏展开
- 外部函数调用及参数
- 可写指针
- memory-mapped IO
- 源码位置和处理顺序

分析结果统一转换为与 Clang 节点解耦的 FunctionIR。

### 5.3 测试用例划分

根据 Profile 对源码结构应用规则，包括：

- 类型最小值和最大值
- 常量边界及边界 ±1
- 变量间相等、不等和大小比较
- AND 条件逐项变化，其他条件固定为 TRUE；OR 条件逐项变化，其他条件固定为 FALSE
- 当 `mcdc_enabled=true` 时，对每个可达原子条件生成独立影响组合，并保留组合证据
- `else if` 各分支及优先级
- `switch` 的 case、default、MIN、MAX及可选 case ±1
- 数组固定索引及未修改元素确认
- Stub 调用次数、参数和返回值
- 循环次数和相关输出
- 根据 Profile 决定是否生成演算式相关用例

超出类型范围的值应删除，重复用例应合并，但必须保留规则追踪信息。

### 5.4 历史用例参与方式

历史测试用例用于：

1. 验证同类 AST 结构和同一 Profile 的用例划分是否一致。
2. 验证 CSV 列选择、排列、注释和 Stub 惯例。
3. 发现测试基准与实际项目用例之间的差异。
4. 建立自动化 Golden Test。

历史用例不得直接覆盖 AST 和规则引擎的结果。存在差异时输出差异报告并要求人工审查。
历史用例必须按项目编号、基础 Profile、MC/DC 开关和规则包版本建立成套语料索引；
未能与源码、配置和目标函数对应的文件不得进入训练/归纳语料。

### 5.5 WinAMS CSV 生成

CSV 输出必须满足：

- 第一行为 `mod`。
- 第二行为 `#COMMENT`。
- `mod` 中输入、输出列数与实际列一致。
- 分支使用 `;$L$`、`TRUE`、`FALSE` 结构。
- 每个数据行列数一致。
- 参数、返回值、Stub 和指针列使用 WinAMS 约定名称。
- 无法证明的输入、分支或输出不得进入正式交付 CSV；只能进入诊断报告和意图清单。
- 指针地址可使用确定性的测试地址（默认 `0x1000`），但必须标记为测试夹具值。
- 输出期望值必须有源码、规则、执行证据或人工审批来源，不能用 `0x0` 伪装 oracle。

正式交付 CSV 只允许包含状态为 `VALIDATED` 的用例。需要人工补齐的草稿必须使用
单独的 draft 产物，不得与正式 TestCsv 混用。

### 5.6 Stub 生成

每个需要隔离的外部调用生成 `AMSTB_<callee>`，Stub 仅负责：

- 累加调用次数
- 记录参数
- 写回可写指针
- 返回预设值

命名采用：

```text
CALLCNT_<callee>
ARG<index>_<callee>[CALL_MAX]
PTROUT<index>_<callee>[CALL_MAX]
AMIN_return[CALL_MAX]
```

Stub 中不得加入业务判断。

### 5.7 格式与一致性校验

系统必须校验：

- CSV 编码和换行
- `mod`、`#COMMENT` 和数据列数
- `$L$` 格式
- CP932 字符可编码性
- Stub 命名和类型
- `CALL_MAX` 容量
- Profile、规则、源码节点和生成用例的追踪关系

校验失败时不得标记生成成功。

### 5.8 Golden 与基准资料分层

Golden 分为三类，不能混用：

1. **格式 Golden**：从《単体テスト項目基準書》抽取的编码、换行、`mod/#COMMENT/$L$`、
   列角色和排列约束。
2. **语义 Golden**：从成套历史用例抽取的边界、分支组合、输入角色、Stub 契约和
   输出 oracle；以语义签名比较。
3. **执行 Golden**：WinAMS 运行产生的 XML、结果 CSV、覆盖率和报告；只用于执行
   闭环验证。

格式规则的来源必须记录到工作簿 sheet/cell 或基准书章节；历史样本只能作为规则
证据和反例，不能绕过 AST、Profile 和规则审批。

### 5.9 PSD再構築样板的基准映射

首个样板项目 `N-O2504-PHD-020` 使用 `PSD再構築` 基础 Profile，并启用
`mcdc_enabled=true`。基准书中的以下章节作为该 Profile 的初始规则证据：

- `0-2`：u1/u2/u4/u8、s1/s2/s4/s8、f4/f8、enum 的值域与最小/中间/最大值；
- `1-1`～`1-4`：变量赋值、常量赋值、数组写入和寄存器/I/O 写入；
- `3-1`～`3-3`：Stub 调用次数、返回值和参数；
- `4-1`～`4-5`：变量比较、常量边界、大小比较、AND/OR 独立条件和数组比较；
- `6-1`～`6-2`：else-if、switch、循环次数和测试模式排列。

其中 `4-4` 的 AND/OR 规则在 `mcdc_enabled=true` 时必须实例化为逐原子独立变化；
`mcdc_enabled=false` 时仍执行基础分支覆盖，但不额外生成 MC/DC 组合。

本样板中四个已配对函数仅作为规则学习和验证语料，不构成交付范围；交付对象是
带 `PSD再構築-v1` 版本元数据的规则包。规则包审批前必须保持 `candidate`，
样本 TestCsv 只能作为格式、语义和执行证据。

---

## 6. Agent 使用边界

正式生成主链路不需要 Agent。

Agent仅可作为离线辅助能力，用于：

- 从基准文档提取规则草案
- 分析不同 Profile 的差异
- 归纳历史用例惯例
- 解释规则冲突和生成失败原因

Agent结果必须经过人工审核后才能转化为正式规则，不得直接修改交付 CSV。

---

## 7. MVP 范围

### 必须实现

1. 项目编号到基础 Profile、MC/DC 开关和版本化例外的映射。
2. 单文件、单函数 Clang AST 分析。
3. FunctionIR 生成。
4. `if`、`else if`、简单 AND/OR、`switch` 分析。
5. MIN、MAX、边界和边界 ±1 用例生成。
6. 基于 Profile 的规则差异处理。
7. 历史 CSV 的格式/语义 Golden 分层对比。
8. 仅输出 `VALIDATED` 用例的 WinAMS CSV 和 Stub 生成。
9. CP932、CRLF及列数校验。
10. 规则追踪、Profile 证据和差异报告。

### 暂不实现

- 完整符号执行
- 自动推导复杂业务期望值
- 跨复杂表达式、短路副作用或多函数状态的完整 MC/DC 最小化
- 多函数联合状态分析
- 自动执行 WinAMS
- 自动生成 RH850 可执行产物
- Agent 自动修改正式结果

---

## 8. 验收标准

1. 相同源码、配置、项目 Profile、例外版本和工具版本生成字节级一致的结果。
2. 生成 CSV 可被 WinAMS 正确识别。
3. CSV 满足 CP932、CRLF 和列数约束。
4. 每条正式用例可追踪到基础 Profile、MC/DC 开关、例外版本、规则编号和源码节点。
5. 不同 Profile 对同一源码能够生成不同的规定用例划分。
6. 外部调用具有符合契约的 Stub。
7. 与历史 Golden 用例存在差异时，工具能够输出明确的差异报告。
8. 无法确定的业务期望值不进入正式 CSV，只在诊断报告中标记为待人工确认。

---

## 9. 产品结论

本产品的核心交付不是四个样本函数的测试用例，而是版本化、可审批、可复用的
Profile 规则包；样本 CSV 仅用于学习、反例和回归验证。产品最终价值是：

> 按项目测试观点稳定地复现既有测试用例划分，并输出严格符合 WinAMS 约束的覆盖率测试 CSV 和 Stub。
