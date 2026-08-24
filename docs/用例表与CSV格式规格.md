# 用例表与 CSV 格式规格（v0.1 草案）

> 本文档把既定口径固化成机器可实现的规格。凡是口径没说死的细节，用【补充解释 n】标出，集中在第 7 节待确认。

---

## 1. 事实源（既定口径）

1. **通用能力优先**：工具适配多个项目，不绑定任何仓库；目标代码只是输入之一。
2. **Stub 自制**：被测函数内所有被调用函数 stub 化，stub 体只留 `callcnt` 与引数的入出力，不含任何逻辑。普通形参记录用 `ARG<k>_<形参名>`；**指针形参必须区分传入/传出**，传入记录用 `PTIN<k>_<形参名>`，传出值用 `PTOUT<k>_<形参名>[CALL_MAX]` 按调用序设定；stub 返回值用 `CALLRET<k>`（按调用序，**仅当参与被测函数分支判定时才生成**）；被测函数返回值必加（期待列）。
3. **CSV 变量按调用顺序排列**：先 callcnt，然后是被测函数的引数、返回值，再是分支语句的控制变量。指针引数先用 `@<引数名>` 分配地址（如 0x1000），所选地址**之前**的地址段是空的、可以拿来用。
4. **分支语句写注释行**：for / if / else if / while / switch / 三元运算符，每个分支语句一行注释；条件里的宏定义字面值展开，保留类型和真实值。
5. **注释行下写组合分支行**：枚举该分支语句的可能性排列组合，二元写 True/False，多元（多原子条件）简写 T/F；同层同算子去冗余：**`&&` 条件不列全 F 行，`||` 条件不列全 T 行**。
6. **控制变量测满五点**：本身、+1、−1、最大、最小。**±1 填入的前提是该值能进到这个分支语句里**（值域合法且路径可达），不能就不加；**最大最小取"能进入分支判断的最大最小"**（可达域极值，非类型极值）。**"体现区别"指期待值层面**：不同分支的用例要尽量在期待值（返回值/写回全局/callcnt/记录列）上可区分；一个分支下允许多条用例，也允许没有。

## 2. 通用性原则

- 工具输入 = 任意 C 源码树 + **配置头文件**（决定 `#if` 分支与调用集）+ 类型定义头。不写死任何模块名。
- 实测发现：Arctic Core（classic-platform）里 `CANIF_CHANNEL_CNT` 等配置宏**只有使用处、没有定义处**——真实项目里配置头由配置工具生成后注入编译。因此配置头必须是一等输入，每个 ECU 项目一套，`#if` 的解析结果随配置变化，调用集（进而 stub 集、CSV 列集）也随之变化。

## 3. Stub 生成规范

对被测函数体内的每个被调用函数（按调用顺序编号，从 00 起），生成：

```c
#define CALL_MAX 16   /* 单用例内单 stub 最大调用次数上限，可配置 */

/* ---- 调用#00: F(T1 a1, ..., const T2 *q, T3 *p, ...) ---- */
uint32 callcnt00 = 0;                /* 每用例执行前置 0 */
T1     ARG00_a1[CALL_MAX];           /* 普通形参：入力记录，第 n 次调用写 [n-1] */
T2     PTIN00_q[CALL_MAX];           /* 指针传入：记录指向物值，按调用序 */
T3     PTOUT00_p[CALL_MAX];          /* 指针传出：按调用序设定的写出值（用例设定） */
Tret   CALLRET00[CALL_MAX];          /* 返回值：仅当参与被测函数分支判定时生成；否则省略，stub 返回类型零值 */

Tret F(T1 a1, ..., const T2 *q, T3 *p, ...) {
    ARG00_a1[callcnt00] = a1;        /* 入力记录：先记后增，索引 0 起 */
    PTIN00_q[callcnt00] = *q;        /* 传入指针记录指向物 */
    if (callcnt00 < CALL_MAX) {
        *p = PTOUT00_p[callcnt00];   /* 传出写入：值来自用例按调用序的设定 */
    }
    callcnt00++;
    return CALLRET00[callcnt00 - 1];
}
```

命名与判定规则：

| 形参类别 | 命名 | 语义 |
|---|---|---|
| 普通形参 | `ARG<k>_<形参名>[CALL_MAX]` | 入力记录（实际传入值，按调用序编址） |
| 指针·传入 | `PTIN<k>_<形参名>[CALL_MAX]` | 记录指向物值（`const T*` 等） |
| 指针·传出 | `PTOUT<k>_<形参名>[CALL_MAX]` | 按调用序设定的写出值 |
| 返回值 | `CALLRET<k>[CALL_MAX]` | 按调用序设定；**仅当参与被测函数分支判定时生成**（不控制条件语句的返回值不加） |

- `<k>` = stub 编号，按被测函数内调用顺序从 00 起。被测函数自己的返回值不受此条限制，必加期待列。
- **指针传入/传出判定**：`const T*` → 传入；非常量指针看真实被调函数是否写入该指向物（或被测函数调用后是否读取）→ 写入即传出；静态判定不了 → LLM 兜底（既定介入点）。
- **无任何逻辑**：不判断、不分支；有状态 stub（依赖调用次数/顺序）是 LLM 介入点，另行生成后替换骨架。
- static 内部函数同样 stub 化（函数隔离模式），保证被测单元只有被测函数本身。
- 经函数指针调用的（如 `CanIfUserTxConfirmations[..](..)`）：stub 化指针指向的函数类型，callcnt 按调用点计。
- 宏包裹的调用（`VALIDATE_RV`、`DET_REPORT_ERROR`）先展开宏再识别调用，展开结果同时进分支注释行（见 §4）。

## 4. CSV 布局规范

### 4.1 列区块顺序（按调用顺序）

```
[A] stub 区（按被测函数内调用顺序编号 00 起，每个 stub 一组）
    callcnt<k> → ARG<k>_<形参名> / PTIN<k>_<形参名>（入力记录列）→ PTOUT<k>_<形参名>（传出设定列）→ CALLRET<k>（返回值设定列，仅参与分支判定时）
[B] 被测引数区（按函数签名顺序）
    指针引数：先以 @<引数名> 分配地址（如 @ptr = 0x1000），所选地址之前的地址段空闲、
    可用作指向物数据区；指向物的值在该地址区上设定
[C] 被测返回值（期待值）
[D] 分支控制变量：不设重复列——是引数 → 在 [B] 设值（C 语言同名即同一变量）；
    是全局 → 在全局列设值；来自 stub 返回值/传出 → 在 [A] 对应设定列。
    控制变量分析只决定"哪个区块的哪一列参与组合"，不新增设定列。
```

### 4.2 行类型（首列符号区分，机器可解析）

| 行 | 首列标记 | 说明 |
|---|---|---|
| 分支注释行 | `#` | 每个分支语句一行：`# 编号 | 类型 | 原文 | 宏展开后 | 变量:类型,真值` |
| 组合分支行 | `%` | 紧贴注释行下方：单原子 `True` / `False`；多原子同层同算子组合写 `T`/`F`，**`&&` 不列全 F 行、`||` 不列全 T 行**；switch 写每个 case 一行 + `default(其他值)` |
| 数据行 | 用例ID | 每行一个测试用例，各设定/期待列给具体值 |

### 4.3 注释行内容（宏展开，保留类型和真实值）

例（真实代码）：

```
# B02 | if | VALIDATE_RV((ControllerId < CANIF_CHANNEL_CNT), CANIF_SETPDUMODE_ID, CANIF_E_PARAM_CONTROLLERID, E_NOT_OK)
       → 展开: if(!(ControllerId < 2)) { Det_ReportError(CANIF_MODULE_ID,0,CANIF_SETPDUMODE_ID,CANIF_E_PARAM_CONTROLLERID); return 1; }
       | ControllerId: uint8 | CANIF_CHANNEL_CNT=2 (配置值)
% True
% False
```

展开规则：

- 枚举常量 → `(字面值, 枚举名)`：`CANIF_GET_ONLINE` → `3`
- 宏字面值 → 真实值 + 类型：`TRUE` → `1 (boolean)`
- 函数宏（`VALIDATE_RV`、`DET_REPORT_ERROR`、`IS_EXTENDED_CAN_ID` 等）→ 递归展开成代码后进注释行
- `#if` 块：由配置头解析，非生效代码不进任何行；注释行标注生效配置（如 `[CFG: CANIF_PUBLIC_DEV_ERROR_DETECT=STD_ON]`）

### 4.4 控制变量边界值（五点，去重）

对每个原子条件的边界值 v：取 `{v−1, v, v+1} ∪ {可达域最小, 可达域最大}`，并按三条前提过滤：

- **±1 填入前提**：v±1 必须能进到该分支语句里——值域合法（boolean 无 2；枚举域内）且路径可达（前置校验/分支未把它挡掉）。不能进就不加。
- **max/min 取"能进入该分支判断的最大最小"**：分支可达值域的极值，不是类型极值。例：`CanIf_SetPduMode` 的 B02 校验 `ControllerId < 2` 通过后，下游所有分支的 ControllerId 可达域只剩 {0,1}——下游 min=0、max=1；255 只对 B02 本身有意义（它能到达 B02 并走 False 路径）。
- **体现区别（期待值层面）**：用例设计应尽量让走**不同分支**的用例在期待值（返回值、写回全局、callcnt、ARG/PTIN 记录列）上可区分——审查者从期待值就能看出走了哪个分支。**同一分支允许多条用例**（如边界点 v 与 v+1 都走 False，照加不删），也允许没有（如 ±1 越界进不了分支时，该分支从这点得不到用例）。

| 类型 | 值域 |
|---|---|
| uint8 | 0..255（再按分支可达域收窄） |
| boolean | 0 (FALSE) / 1 (TRUE)，±1 超域即不加 |
| 枚举（底层 uint8） | 0..末项值；default 用域外值（末项+1 / 0xFF）触发 |

- switch 变量：每个 case 值本身 ±1（域内且可达才加）、可达域 min/max、一个"所有 case 之外"的值（测 default）。
- **可达域判定**：前置条件的区间传播，由解析层完成；判不了 → LLM 兜底（既定介入点）。
- 多输入组合笛卡尔积，超阈值降 pairwise（既定方案不变）。

## 5. 三元 / 循环的注释行

- 三元 `c ? a : b`：等同 if/else，注释行类型写 `ternary`，组合行 True/False。
- for：注释行写循环变量、初值、边界、步长（宏展开），组合行写 `进入/不进入`（True/False）与边界次 ±1；循环体内含分支的照常拆原子条件。
- while / do-while 同 for。

## 6. 手写示例：CanIf_SetPduMode（golden 样例）

源码：`examples/classic-platform/communication/CanIf/src/CanIf.c` L1342，真函数，含 switch 七 case + default、case 内多原子 else-if、VALIDATE_RV 宏校验、全局读写、`#if` 配置块。

**前提配置**（示例假设，写死在样例头）：

```
CANIF_CHANNEL_CNT=2  CANIF_PUBLIC_DEV_ERROR_DETECT=STD_ON
CANIF_PUBLIC_PN_SUPPORT=STD_OFF  CANIF_PUBLIC_TX_BUFFERING=STD_OFF
```

→ `#if` 内的 `setPnFilterEnable`、`ChannelOnEnterPDUMode` 不参与编译，调用集只有 `Det_ReportError`（经 VALIDATE_RV）。

**枚举真值**（CanIf_Types.h 实测）：

- PduModeRequest（CanIf_PduSetModeType/uint8）：SET_OFFLINE=0, SET_RX_OFFLINE=1, SET_RX_ONLINE=2, SET_TX_OFFLINE=3, SET_TX_ONLINE=4, SET_ONLINE=5, SET_TX_OFFLINE_ACTIVE=6
- currMode（CanIf_PduGetModeType/uint8）：GET_OFFLINE=0, GET_RX_ONLINE=1, GET_TX_ONLINE=2, GET_ONLINE=3, GET_OFFLINE_ACTIVE=4, GET_OFFLINE_ACTIVE_RX_ONLINE=5

产物：

- `examples/golden/CanIf_SetPduMode/CanIf_SetPduMode_stubs.c` —— 按 §3 生成的 stub
- `examples/golden/CanIf_SetPduMode/testdata.csv` —— 按本规格的 CSV（注释行 + 组合行全量，数据行代表性 15 条；全量枚举由 M2 脚本生成）

## 7. 拍板记录

**第一批（2026-08-24）**

1. stub 指针形参必须区分传入/传出：传出值用 `PTOUT<k>_<形参名>[CALL_MAX]` 按调用序设定；普通形参记录用 `ARG<k>_<形参名>`。
2. 被测函数返回值列 = **期待值**。
3. 控制变量是引数时，直接在引数列设值（C 语言同一变量，不存在同名冲突问题）；**指针引数先用 `@引数名` 分配地址**（如 0x1000），其前地址段空闲可用。
4. 组合行去冗余：`&&` 条件去掉全 F 行（无意义），`||` 条件去掉全 T 行（无意义）。
5. switch default 用"其他值"表达，数据行以 max+1 / 0xFF 类非法值触发。

**第二批（2026-08-24）**

6. `ARG` 同样按 `[CALL_MAX]` 编址，与 PTOUT 对称。
7. stub 返回值列命名 `CALLRET<k>`（按调用序 `[CALL_MAX]`）；**仅当该返回值参与被测函数分支判定时生成**，不控制条件语句的返回值不加；**被测函数返回值必加**（期待列）。
8. 传入指针记录命名 `PTIN<k>_<形参名>`。
9. 边界值新前提：±1 必须能进到该分支语句里（值域合法 + 路径可达），不能就不加；max/min 取能进入分支判断的最大最小（可达域极值，非类型极值）。**"体现区别"指不同分支的用例尽量在期待值上可区分**（不是拿判定翻转去筛边界值）；一个分支下允许多条用例或没有。

（无剩余待确认项）

---
配置宏无定义的发现：`CANIF_CHANNEL_CNT` 全仓库仅 CanIf.c 使用处、无定义处，证实配置头必须外置输入。
