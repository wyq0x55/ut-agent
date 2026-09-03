# PSD再構築 Ver.1.6 — 1-4 寄存器、I/O 端口的值设置

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**寄存器、I/O 端口的值设置**
- 原表名称："レジスタ、I/Oポートへの値設定"
- 稳定 ID：`1-4`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!C84:J90`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：使用设计书地址、访问宽度和尺寸设置寄存器/I-O。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 84

- `C84` (shared_string): "1-4"
- `D84` (shared_string): "レジスタ、I/Oポートへの値設定"

### Row 85

- `D85` (shared_string): "設計書(アドレス一覧表等)のアドレスを設定し、実施する"
- `G85` (shared_string): "   "

### Row 87

- `D87` (shared_string): "例．p8 = u1a_val_a ;            ※p8のアドレスは0x3F0、サイズはu1"

### Row 88

- `D88` (shared_string): "入力値"
- `E88` (shared_string): "初期値"
- `F88` (shared_string): "期待値"

### Row 89

- `D89` (shared_string): "u1a_val_a"
- `E89` (shared_string): "0x3F0#U1#1"
- `F89` (shared_string): "0x3F0#U1#1"
- `H89` (shared_string): "0x3F0"
- `I89` (shared_string): "#U1"
- `J89` (shared_string): "#1"

### Row 90

- `D90` (number): "255"
- `E90` (number): "0"
- `F90` (number): "255"
- `H90` (shared_string): "アドレス"
- `I90` (shared_string): "型"
- `J90` (shared_string): "サイズ"
