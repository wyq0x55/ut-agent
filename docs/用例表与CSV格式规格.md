# WinAMS 用例表与 stub 格式规格（v1.0）

本项目的交付对象是 WinAMS 工程，不再把项目早期的 `case_id` 自定义 CSV
作为主格式。`ut-agent gen` 直接生成 WinAMS 可识别的 stub 源码和
`TestCsv` 输入文件。

## 1. 输入与确定性

解析输入固定为三件套：C 源码、include 路径、配置宏/配置头。相同输入必须
产生相同的 FunctionIR、stub 和 CSV；核心路径不访问网络、不调用 LLM、不使用
随机数或当前时间。

配置宏通过 `-D NAME=VALUE` 或 `--include-config` 传入。WinAMS 的 `mod` 行
不承载自定义 CFG 注释，实际编译时必须把同一组配置传给 ARM GCC 和 WinAMS
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

边界值来自 `cases.boundary` 的确定性枚举。无法从源码推导的输入默认写
`0x0`，指针地址默认写 `0x1000`，输出期望默认写 `0x0`；这些值是可执行的
WinAMS 初始数据，不宣称是业务正确期望值。业务期望值必须由 WinAMS 执行结果、
寄存器模型或人工审查回填。

已有 WinAMS TestCsv 可通过 `--reference-csv` 作为模板：生成器读取其 `mod`、
`#COMMENT` 和输入数据行，分支条件仍从当前源码重新生成；这样可以复用项目
已经验证过的寄存器初值/期望值。若不提供模板，输入默认值为 `0x0`，需要在
WinAMS 或人工审查阶段补齐业务数据。

## 4. ARM GCC 产物

参考 `Soft.out` 是 RH850/V800 GHS ELF，不能直接用 ARM 链接器链接。项目现在
使用 Arm GNU Toolchain 的 `arm-none-eabi-gcc`：

```bash
ut-agent arm-build Dma.c -I <全部 include 目录> \
  -o .build/p_vog_dma_init.arm.elf --entry p_vog_dma_init
```

如果同时把生成的 `<function>_stubs.c` 作为输入，构建器会自动发现并保留
`AMSTB_` 符号，避免 `--gc-sections` 把 WinAMS stub 丢掉：

```bash
ut-agent arm-build Dma.c <function>_stubs.c -I <全部 include 目录> \
  -o .build/<function>.winams.out --entry <function> \
  --omf-output .build/<function>.winams.xlo
```

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
  --reference-csv <参考 TestCsv> \
  --out .build/winams/<function>
```

输出：

- `<function>_stubs.c`：WinAMS `AMSTB_` stub；
- `<function>_testdata.csv`：WinAMS CP932/CRLF TestCsv；
- ARM ELF 由同目录下的 `arm-build` 命令生成，避免把编译器特定参数混入 CSV。

## 6. 参考项目自检记录

以 `N-O2602-MVC-234/work/Soft` 为例：

- 已复制到 WSL 的 `.build/reference/N-O2602-MVC-234/work/Soft`，源文件哈希与
  Windows 文件一致；
- `Dma.c::p_vog_dma_init` 可用 97 个源码 include 目录解析，识别 4 个调用和
  3 个条件分支；
- 参考 `TestCsv/p_vog_dma_init.csv` 已作为列模板验证 `mod/#COMMENT/;$L$`；
- ARM GCC 已生成带 `.debug_info`、`.debug_line` 的 ARM EABI5 ELF；
- 原始 GHS `Soft.out` 仅作为 WinAMS 参考，不能冒充 ARM GCC 产物。

## 7. 规格变更记录

2026-08-25：根据 N-O2602-MVC-234 的实际 WinAMS 工程，将项目主输出从早期
自定义 `case_id/%/# CFG` 和 `callcnt00/CALLRET00` 契约替换为参考工程实际使用的
`mod/#COMMENT/;$L$`、`AMSTB_`、`CALLCNT_`、`ARG`、`PTROUT`、`AMIN_return`。
旧格式仅保留在 host 回放器内部，不能作为 WinAMS 交付文件。
