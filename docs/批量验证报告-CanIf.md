# 批量通用性验证报告（一）：CanIf.c 全函数 + PduR 探针

> 日期：2026-08-24 · 流水线版本：M2.5 闭环 + 指针表 stub + 配置 fixture ·
> 命令：`ut-agent batch .../CanIf.c -D CANIF_CHANNEL_CNT=2`

## 结果总览（第二轮，指针表 stub 与配置 fixture 接入后）

| 状态 | 数量 | 说明 |
|---|---|---|
| **OK（全自动）** | **11/12** | 解析→枚举→fixture→编译→执行全链路无人工 |
| FAIL_GEN·双重指针形参 | 1 | `linearSearch(const CanIf_RxPduConfigType **rxPdu)`——唯一残留介入点 |

| 函数 | 状态 | 参数 | 指针参数 | stub | 分支 | 原子 | 用例 | 备注 |
|---|---|---|---|---|---|---|---|---|
| CanIf_SetPduMode | OK | 2 | - | 1 | 16 | 20 | 480 | golden 对照全过 |
| CanIf_SetControllerMode | OK | 2 | - | 2 | 7 | 6 | 150 | 配置 fixture |
| CanIf_GetControllerMode | OK | 2 | ControllerModePtr | 1 | 3 | 3 | 20 | 指针出力回读 |
| CanIf_GetPduMode | OK | 2 | PduModePtr | 1 | 3 | 3 | 20 | 指针出力回读 |
| CanIf_Transmit | OK | 2 | PduInfoPtr | 3 | 18 | 20 | 0 | 指针表 stub；用例组合待配置数据 |
| CanIf_RxIndication | OK | 4 | canSduPtr | 4 | 11 | 15 | 0 | 指针表 stub；同上 |
| CanIf_TxConfirmation | OK | 1 | - | 3 | 5 | 8 | 0 | 指针表 stub；同上 |
| CanIf_ControllerModeIndication | OK | 2 | - | 3 | 4 | 4 | 2 | 指针表 stub |
| CanIf_ControllerBusOff | OK | 1 | - | 3 | 4 | 2 | 6 | 指针表 stub |
| CanIf_Init | OK | 1 | configPtr | 1 | 2 | 2 | 2 | 配置 fixture |
| ControllerToChannel | OK | 2 | channel | 0 | 3 | 2 | 1 | 配置 fixture；控制变量为配置成员 |
| linearSearch | FAIL_GEN | 4 | rxPduCfgPtr,rxPdu | 0 | 2 | 4 | 1 | 双重指针出参 |

注：用例=0 的函数编译执行链路已通，但控制变量全部来自配置表/局部链，无可设定组合——
需要真实配置数据（对应 rows>0 的完整覆盖），已在 notes 标注。

## 第二轮新增机制（本轮工程内容）

1. **指针表安装 stub**（救活 5 个分发表/回调数组函数）：
   - 解析器：token 分析得出表全局名/成员名/实参类型，去重键 base.member
   - fixture：`#define 表名 → ut_agent_fix_表名`（可写零初始化 fixture，extern 原声明不动）
   - driver：主循环前安装 `fix.成员 = stubNN_表_成员;`（回调数组为全量安装）
   - CSV：表 stub 的标量实参上 ARG 列（callcnt{k}(期待·指针表)）
2. **配置表 fixture**（救活 3 个配置表指针函数）：
   - cindex 枚举配置结构体字段 → 指针字段配零初始化数组（固定 4 项）+ designated 接线
   - `ut_agent_config_init()` 在 driver 主循环前执行
   - 深层（三层以上）指针仍为空，触及者如实 FAIL_RUN
3. 修复：`_base_type` 只剥一层星（双重指针指向物 = T*）；设定/写回表达式含
   非参数局部变量下标时跳过并记 note；after fallback 无来源列时用字面 0

## 结论：框架边界地图（更新）

- 全自动形态新增：**分发表/回调数组调用、配置表指针 deref（两层）、
  局部变量下标的全局写回（跳过设定、期待列以设定值代）**
- 唯一残留介入点：**双重指针形参**（PTOUT 两层数组 + @地址约定扩展到指针的指针）
- rows=0 的三个函数（Transmit/RxIndication/TxConfirmation）：链路通，
  完整用例需真实配置数据——与 SILS 方案的"配置构筑闭环"思路一致

## 第二模块探针：PduR_Logic.c

- 诊断错误仅 **1 个**：缺 `PduR_Cfg.h`（生成配置）——其余 208 个函数全可解析。
- 结论：接入 PduR 只需新建 `configs/cp_pdur/PduR_Cfg.h` 最小配置包，
  框架本体零改动。**配置注入包 = 每模块一次性成本**。

## 本轮修复清单（全部有测试锁定）

- 指针引数全链路：@地址行 / const→指向物设定 / 非 const→`_out` 期待列 / driver 取址回读
- 未命名形参（原型 `f(uint8, uint8)`）→ `arg{i}` 合成名
- PTOUT/PTIN 数组元素类型剥 `*`/const
- static 被调函数 stub 带 `static` 前缀 + 前置原型块（消除隐式声明冲突）
- 函数窗口预处理平衡（孤儿 #endif/#else 清理、未闭合 #if 补 #endif）
- void 返回函数的 driver 调用/打印
- 结构体指向物零初始化（`{0}`），数值型指向物接用例值
- 配置表成员控制变量（`->`）标记 source=config 不可设定
- 分发表调用误判为函数 → 按"callee 命中全局变量名"纠正为指针表调用
- batch 自动为 gcc 剔除 libc_stub；函数清单按主文件过滤（排除 libc 函数）
