# SILS 测试（Synopsys Silver）AI 自动化方案

> 基于现有平台 `slivertesttool`（Flask + PostgreSQL + Huey + Silver 池化执行）接入 AI，使人工只负责**审核与提议**。

---

## 1. Silver 与 SILS 背景

- Silver（Synopsys，原 QTronic）：虚拟 ECU / 软件在环（SIL）仿真工具，通过将部分硬件接口 stub 化，使 ECU 的 C 源码以纯软件方式在 PC 上运行。
- SBS 文件：Silver 的仿真环境构筑脚本，描述源码模块、登记变量、stub 化硬件接口。测试变量必须加入 SBS 才能被设定。
- SILS 测试：在 Silver 环境中按测试观点驱动被测软件，校验入力值/期待值。

## 2. 项目操作流程（原始人工流程）

1. 配置 SBS 文件，用源代码构筑 Silver 测试环境
2. 从详细设计书（Word/Excel，含图片表格，按模块规格化、每个模块有专属 ID）抽出测试观点；观点为正例/反例/排列组合，一个 ID 可能对应多个观点
3. 根据观点编写测试手顺：控制入力值、期待值、变量使满足观点的测试目的；测试变量需加入 SBS 才能被设定
4. 测试手顺生成 Python 脚本，放入 Silver 中执行得到结果
5. 结果总结成报告；与观点不一致处提交 issue 报告

## 3. 现有平台能力（slivertesttool 已具备）

| 环节 | 现状 |
|---|---|
| 测试观点导入 | Excel Test Matrix 工作簿导入（preview → commit，支持 upsert 等模式） |
| 手顺编写 | 网页步骤编辑器，`TestItemRow.test_steps`（JSON） |
| 用例生成 | 行数据自动生成 `testcase_<id>.json` + `lib.json` + `constants.json` |
| 执行 | Silver 实例池 + Huey 任务队列，SSE 实时日志 |
| 判定 | 解析 `jdgrslt.log`，按用例段落判 Passed/failed |
| 回写与报告 | verdict 回写矩阵 + `TestRunRecord`，zip 报告下载 |
| 审查 | review/exempt 双状态机、单元格评论 |
| SBS 版本 | `SbsRevision` 模型 |

**结论：执行链路已全自动闭环，唯一空白是无任何 AI 接入。AI 只需补"生成"一段，人工审核走现有界面。**

## 4. AI 接入总体架构

### 4.1 人工只剩两种动作

- **审核**：SBS diff、观点表、手顺/报告/lib，全部在现有 review 状态机中确认
- **提议**：人工标注"这段共同逻辑适合做成 lib"，agent 据此生成

### 4.2 agent 权限边界（全部有机器校验兜底）

| 对象 | 权限 | 校验闭环 |
|---|---|---|
| SBS | 可写（生成/增量补变量） | Silver 构筑通过 |
| 手顺 | 可写 | schema 校验 + dry-run 执行通过 |
| lib | 可写（**仅由人工提议触发**） | lib.json schema + 引用手顺重跑 |
| 观点表/报告/issue | 出草稿 | 人工审核 |

### 4.3 技术形态

- 新增 `app/services/ai/` 模块：provider 抽象（OpenAI 兼容接口，模型可配置），平台可访问外部网络，直接用云端 API
- 公共框架：generate → validate → retry → 待审状态，五个场景复用

---

## 5. 五个 AI 场景

### 5.1 环境构筑（SBS 生成与更新）

- 输入：模块源码 + 上一版 SBS
- agent 基于源码索引生成/更新 SBS：登记模块、变量、stub 化硬件接口
- **校验闭环**：自动跑 Silver 构筑，报错（未定义符号/类型不匹配/重复登记）喂回 agent 修，循环至通过
- 每次变更存新 `SbsRevision` + diff，人审查 diff 后启用

### 5.2 按需补变量（手顺驱动）

手顺生成发现所需变量不在 SBS → 查源码索引确认存在与类型 → 生成 SBS 增量补丁 → 增量构筑验证 → 回到手顺生成。变量登记从"人工预先想全"变为"用到才加、加了就验"，全自动。

### 5.3 观点抽取（设计书 → Test Matrix）

- 脚本（python-docx/openpyxl）抽设计书文本/表格/图片 → LLM 输出**统一观点表**：`观点ID、设计书模块ID、正反例类型、涉及变量、条件、期待值`
- **直接生成符合 Test Matrix 导入模板的工作簿**，走现有 preview → commit 导入链路（落库/校验/去重复用现有代码，改动面最小）
- 一个模块 ID 的正例/反例/排列组合在 prompt 中按规则枚举展开为多行
- 源码索引使设计书模糊描述（"车速信号"）直接落到具体变量名

### 5.4 手顺生成（核心：必须看代码）

**关键前提：手顺中的变量名来自 ECU 源码，必须先读代码才能写手顺。** lib.json/constants.json 只是测试库自身的子程序/常量表，不含源码变量。

**第一层：源码索引（脚本，确定性）**

- libclang 解析源码树：每文件抽出全局变量（名、类型、数组维度）、静态变量、函数签名、结构体成员
- 同时解析 SBS 已登记变量，与 `SbsRevision` 版本绑定，代码变更后刷新
- 存储：`{模块ID/文件 → 变量清单}`，保证 LLM 拿到的变量名真实存在

**第二层：按观点组装上下文（控制 token）**

设计书按模块规格化且有专属 ID → 模块 ID 直接映射源文件/函数，**无需向量检索**。prompt 包含：

1. 观点（条件、正反例、期待行为）
2. 该模块相关函数（按观点关键词匹配函数名/函数内变量筛选，非整文件）
3. 源码索引中该模块的变量清单
4. 测试库子程序/常量表（lib.json / constants.json）
5. 2~3 个人工范例手顺（few-shot，对齐书写习惯）

**校验闭环**：手顺引用的每个变量必须在源码索引/SBS 中存在 → 不存在触发 5.2 补变量 → 生成后 Silver dry-run，跑不通打回重生成 → 人只审核能跑通的。

### 5.5 lib：人工提议、agent 编写

- agent 写手顺时：现有 lib 能复用**必须复用**（硬规则：禁止手写等价逻辑）；没有的逻辑直接内联写进手顺（长一点无所谓，JSON 手顺无运行时代价，仅生成时多 token）
- 人在审核时发现重复出现的共同逻辑，标注"提议入 lib"（哪些手顺、哪段逻辑）
- agent 收到提议后：提取共性逻辑 → 生成 lib 函数（带功能描述供检索）→ 回头改写引用手顺 → lib.json schema 校验 + 手顺重跑 → 待审
- 人审核 lib 函数与改写手顺，确认入库
- 好处：lib 的入库动机是真实复用（已在多个手顺中出现），长出来贴着实际需求，无"为抽象而抽象"的死代码

### 5.6 失败分析与 issue 起草

- 失败时将 `jdgrslt.log` 的用例段落（`extract_case_section` 已能切分）+ 观点 + 手顺喂给 LLM
- 生成差异原因分析草稿，挂到 `CellComment`，人审核后确认成 issue

---

## 6. 落地顺序

1. **源码索引层**（前置依赖，纯脚本，无 LLM）
2. **手顺生成**（改动最小、闭环最短、审查界面现成）→ 用真实模块评估生成质量
3. **环境构筑 + 按需补变量**（SBS 生成闭环）
4. **观点抽取**（依赖设计书质量，先拿 2~3 个典型模块试抽，评估漏抽/误抽率，确定人工审核放在观点表这步）
5. **lib 提议流 + 失败分析**

## 7. 风险与注意

- **MC 级复杂观点**：排列组合多时 LLM 一次到位率有限，靠校验闭环多轮兜底
- **token 控制**：函数级筛选上下文、固定小 prompt、便宜模型可胜任大部分生成
- **审查一致性**：可枚举部分（正反例、边界）尽量规则化生成，保证项目间输出一致
- **SBS 自动修改的安全性**：版本化（SbsRevision）+ diff 审核 + 构筑验证三重兜底

## 附：与单元测试方案的对照

| | 单元测试（WinAMS） | SILS（Silver） |
|---|---|---|
| 确定性主体 | 边界值/MC/DC 用例枚举 | 执行/判定/回写链路（平台已有） |
| LLM 主战场 | 覆盖率闭环、变量映射兜底 | 设计书观点抽取、手顺生成、SBS 构筑 |
| 校验闭环 | 覆盖率结果 | Silver 构筑 + dry-run |
| 人工 | 审查报告 | 审核 + 提议（lib） |

参考：[Synopsys Silver](https://www.synopsys.com/verification/virtual-prototyping/silver.html) · [MathWorks Silver 集成](https://www.mathworks.com/products/connections/product_detail/silver.html) · [dSPACE SIL Testing](https://www.dspace.com/en/inc/home/applicationfields/foo/sil_testing.cfm)
