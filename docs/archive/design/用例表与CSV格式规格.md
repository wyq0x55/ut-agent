> Historical document. Not normative. Current architecture: ../../architecture.md
# WinAMS 用例表与 stub 格式规格（v1.0）

本项目的交付对象是 WinAMS 工程，不再把项目早期的 `case_id` 自定义 CSV
作为主格式。`ut-agent gen` 直接生成 WinAMS 可识别的 stub 源码和
`TestCsv` 输入文件。

## 1. 输入与确定性

源码解析输入固定为三件套：C 源码、include 路径、配置宏/配置头。相同输入必须
产生相同的 FunctionIR、stub 和 CSV；核心路径不访问网络、不调用 LLM、不使用
随机数或当前时间。

规则引擎的证据输入范围允许包含完整项目的构建产物，但必须只读使用，不能将其与
源码语义混为一谈：
源码/AST 是条件、数据流和配置初始化值的主依据；`Soft.map` 用于符号、函数表和
地址解析；`Soft.mot` 用于机器码、代码布局和分支实现的交叉确认；`Soft.out` 用于
链接结果、符号/DWARF 和目标数据布局；`Soft.out.xlo` 用于 WinAMS 对象加载及运行
兼容性确认。原版 `TestCsv`、`DefineVar.dat`、`Output` 仍是规则验证的对照样本，
不得直接复制为新生成结果。

配置宏通过 `-D NAME=VALUE` 或 `--include-config` 传入。WinAMS 的 `mod` 行
不承载自定义 CFG 注释，实际编译时必须把同一组配置传给目标编译器和 WinAMS
工程。

## 2. WinAMS stub 契约

参考文件：`Soft/src/AMSTB_SrcFile.c`。每一个被测函数调用都生成一个
`AMSTB_<callee>` 定义；被测源码中的调用仍然使用 `<callee>`，由 `.amsy`
中的 `STB_PREFIX=AMSTB_` 接管。

文件前导采用：

```c
#define WINAMS_STUB
#ifdef WINAMS_STUB
#define CALL_MAX  5
```

`CALL_MAX` 是可配置的调用序列容量。参考工程为 5，调用次数超过 5 的函数
必须在生成时用 `--call-max` 调大，例如 DMA 初始化函数使用 30。

每个 stub 的命名和语义如下：

| 项目 | 格式 | 语义 |
|---|---|---|
| stub 名 | `AMSTB_<callee>` | WinAMS 的 stub 前缀接管目标 |
| 调用计数 | `CALLCNT_<callee>` | 从 1 开始的调用次数 |
| 普通参数 | `ARG<参数序号>_<callee>[CALL_MAX]` | 按调用序记录实参 |
| 可写标量指针 | `PTROUT<参数序号>_<callee>[CALL_MAX]` | 每次调用写回的值 |
| 只读/非标量指针 | `PTROUT<参数序号>_<callee>[CALL_MAX]` | 记录传入地址 |
| 非 void 返回值 | `AMIN_return[CALL_MAX]` | 按调用序设置 stub 返回值 |

参考 stub 的体逻辑只有“递增计数、记录参数、写回指针、返回数组值”，不能
加入业务判断。带调用顺序业务行为的 stub 属于后续人工/LLM 介入点，不能混入
确定性生成器。

示例：

```c
u1 AMSTB_Read(u2 address, u1 *value) __attribute__((used));

u1 AMSTB_Read(u2 address, u1 *value)
{
    static volatile u1 CALLCNT_Read;
    static volatile u2 ARG00_Read[ CALL_MAX ];
    static volatile u1 PTROUT01_Read[ CALL_MAX ];
    static volatile u1 AMIN_return[CALL_MAX];

    CALLCNT_Read++;
    ARG00_Read[CALLCNT_Read - 1] = address;
    *value = PTROUT01_Read[CALLCNT_Read - 1];
    return AMIN_return[CALLCNT_Read - 1];
}
```

## 3. WinAMS TestCsv 契约

CLI 写出的文件编码为 CP932，换行是 CRLF。第一行必须是 WinAMS `mod` 行，
第二行是 `#COMMENT`：

```text
mod,"Dma.c/p_vog_dma_init","p_vog_dma_init 単体テスト",2,2,,,,CPP,,,"",0
#COMMENT,"input_0","input_1","output_0","output_1"
```

`mod` 第 4 个字段是输入列数，第 5 个字段是输出列数。`#COMMENT` 的前半部
必须与输入列数相同，后半部与输出列数相同。列名使用 WinAMS 约定：

输入、输出两侧的列顺序也是契约，不能按变量名重新排序：先按首次出现的
调用顺序添加各 stub 的 `CALLCNT`，再添加被测函数引数；其后按源码分支
所需的外部状态和可观察值添加全局、memory、stub 参数/返回值等 I/O；被测
函数的返回值如果存在，必须作为输出侧最后一列。函数体自动局部变量（包括
函数作用域 `static`）不是外部测试 I/O，不得进入 `#COMMENT`。

- 被测函数标量参数：`param`；指针地址：`@param`。
- stub 参数：`AMSTB_SrcFile.c/AMSTB_<callee>@ARG00` 等。
- stub 返回值：`AMSTB_SrcFile.c/AMSTB_<callee>@AMIN_return[0]`。
- 指针写回：`*param`；被测函数返回值：`source.c/function@@`。
- 全局写回：使用 IR 中的全局表达式。

分支和数据行使用 WinAMS 的 `$L$` 语法：

```text
;$L$,if ( condition )
;$L$,TRUE
,0x0,0x0,0x0,0x0
;$L$,FALSE
,0x0,0x0,0x0,0x0
```

边界值来自 `generation.boundary` 的确定性枚举。无法从源码推导的输入默认写
`0x0`，指针地址默认写 `0x1000`，输出期望默认写 `0x0`；这些值是可执行的
WinAMS 初始数据，不宣称是业务正确期望值。业务期望值必须由 WinAMS 执行结果、
寄存器模型或人工审查回填。

默认 `ut-agent project` 不读取已有 WinAMS TestCsv。生成器从 Soft 的
AST/预处理记录生成 `mod`、`#COMMENT`、分支条件和输入数据；输入默认值为
`0x0`，需要在 WinAMS 或人工审查阶段补齐业务数据。原版 TestCsv 仅供
显式的只读对照和规则归纳使用，不能作为 gen/project 的生成输入。后续规则引擎的产物分析阶段
可显式读取 `Soft.map`、`Soft.mot`、`Soft.out` 和 `Soft.out.xlo`，用于规则推导和
结果交叉验证。

## 4. RH850 编译产物（待决策）

`Soft.out` 是 RH850/V800 GHS ELF。当前目标是 RH850，新的 RH850 编译器方案
尚未拍板，因此 AST-only 生成可先使用 `--no-build` 完成源码、TestCsv、stub、
DefineVar 和 `.amsy` 工程生成；不把 `arm-none-eabi-gcc` 冒充 RH850 编译器。

`arm-build` 仅保留为独立的 ARM 示例命令，不属于本 RH850 工程生成链路。
待 RH850 编译器方案确定后，再补充对应的 `.out/.xlo` 产物步骤。

`--omf-output` 调用 WinAMS 安装目录中的 `armgccomf.EXE`，将 GCC ELF 转成
WinAMS 的 `.xlo`；不指定时仍保留原始 ELF，适合只检查编译和 DWARF。

构建器启用 `-g`、`-ffunction-sections`、`-fdata-sections`、
`--gc-sections`，并默认允许未解析的硬件符号。这样保留被测函数和 DWARF
源位置，避免 RH850 ISR 等与被测函数无关的目标代码阻塞 WinAMS 单元产物。
`--strict-link` 可用于要求完整链接。

Windows 安装的 GCC 从 WSL 调用时由 PowerShell 转发，以保证 GCC 能找到同目录
的 `cc1`、`collect2` 等组件。

## 5. 生成命令

```bash
ut-agent gen <source.c> -f <function> \
  -I <include> -D NAME=VALUE \
  --call-max 30 \
  --out .build/winams/<function>
```

输出：

- `<function>_stubs.c`：WinAMS `AMSTB_` stub；
- `<function>_testdata.csv`：WinAMS CP932/CRLF TestCsv；
- RH850 `.out/.xlo` 产物暂不由本链路生成，避免把未确定的编译器方案混入 AST/TestCsv。

### 5.1 WinAMS IO 登录副文件

当前 WinAMS 版本没有公开的 `DefineVar.dat`/IO 登录命令行选项；
`-InputVar_*`、`-OutputVar_*` 和 `-MBToutVarDef` 属于自动测试数据或 MBT
配置，不等价于 IO 登录。`ut-agent project` 因此确定性生成每个函数目录下的
`DefineVar.dat`，并生成 `WinAMS.INI` 的 `DefVarFile` 指向该文件。

默认 project 流程从 FunctionIR 的 memory-mapped IO 记录生成 `DefineVar.dat`：
地址来自 Soft 源码中的整数宏定义，访问宽度来自 `*_read_reg16`、
`*_write_reg32` 等源码调用。只有显式 reference 模式才读取原工程的
`DefineVar.dat`，用于旧 RH850 产物兼容性回放。

`DefineVar.dat` 只登记最终选中的 memory-mapped IO。普通全局变量、指针变量和
stub 的调用次数/参数不生成空定义记录；它们如果影响测试行为，仍可出现在
TestCsv 的输入/输出列中。寄存器名称的 `U1/U2/U4/U8` 前缀与实际 WinAMS IO
登录宽度一致，例如源码宏 `U4L_DMA_REG_ICDMA04` 通过 16 位 helper 访问时
生成 `U2L_DMA_REG_ICDMA04`。

变量选择采用确定性的最小集合：优先保留条件语句作用域内访问的寄存器，
每种访问宽度只取首次出现的代表；寄存器 helper 已由地址变量表示时不再
重复生成 stub 输入列；未选中的寄存器 helper 也不单独生成 stub 输入列。若被测函数已有返回值或可写指针结果，则不把仅写寄存器
重复列入测试变量；无返回/无指针的初始化函数保留寄存器作为可观测输出。能从
源码配置初始化器求值的 `const` 数组成员不生成
设定列，恒真/恒假分支仍输出 `$L$` 标记，并在不可达侧保留说明。
其中恒真分支的 WinAMS 标签使用原项目约定：
`FALSE デッドコードがあった為、この分岐に入ることができません`。

## 6. 参考项目自检记录

以 `N-O2602-MVC-234/work/Soft` 为例：

- 已复制到 WSL 的 `.build/reference/N-O2602-MVC-234/work/Soft`，源文件哈希与
  Windows 文件一致；
- `Dma.c::p_vog_dma_init` 可用 Soft 内部源码/include 目录解析，识别函数调用、
  条件分支以及 memory-mapped IO 地址宏；
- `TestCsv/p_vog_dma_init.csv` 由 AST-only 路径独立生成，golden 只作为显式
  只读对比物；
- RH850 新编译器方案仍待决策，AST-only 结果不包含新的 `.out/.xlo`；
- `Soft.map`、`Soft.mot`、`Soft.out`、`Soft.out.xlo` 均可作为规则引擎的只读证据
  输入；它们不应被改写，也不替代源码 AST 对程序语义的判断。

## 7. 规格变更记录

2026-08-25：根据 N-O2602-MVC-234 的实际 WinAMS 工程，将项目主输出从早期
自定义 `case_id/%/# CFG` 和 `callcnt00/CALLRET00` 契约替换为参考工程实际使用的
`mod/#COMMENT/;$L$`、`AMSTB_`、`CALLCNT_`、`ARG`、`PTROUT`、`AMIN_return`。
旧格式仅保留在 host 回放器内部，不能作为 WinAMS 交付文件。

2026-08-26：调整完整项目产物策略。`Soft.map`、`Soft.mot`、`Soft.out`、
`Soft.out.xlo` 均不再排除，可由规则引擎只读引用；分别用于地址/符号、机器码/布局、
链接与调试信息、WinAMS 加载兼容性分析。源码 AST 仍是规则语义主依据，原版
TestCsv/DefineVar/Output 仅作为对照样本，不能直接抄用。
