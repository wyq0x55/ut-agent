# PSD再構築 Ver.1.6 — 1-1 左右两边都是变量时的赋值

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**左右两边都是变量时的赋值**
- 原表名称："右辺、左辺とも変数の場合"
- 稳定 ID：`1-1`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!B56:F65`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：变量到变量赋值后确认目标变量状态。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 56

- `B56` (number): "1"
- `C56` (shared_string): "代入文"

### Row 57

- `D57` (shared_string): "観点：期待値どおりに値が変化することを確認する"

### Row 58

- `C58` (shared_string): "1-1"
- `D58` (shared_string): "右辺、左辺とも変数の場合"

### Row 59

- `D59` (shared_string): "入力値は、型の最大値とする。"

### Row 60

- `D60` (shared_string): "代入される変数には、初期値を設定しておく（初期値：通常は代入される変数の型の最小値とする）"

### Row 62

- `D62` (shared_string): "例．u1l_Eprom_ModReq  = u1a_ModReq ;"

### Row 63

- `D63` (shared_string): "入力値"
- `E63` (shared_string): "初期値"
- `F63` (shared_string): "期待値"

### Row 64

- `D64` (shared_string): "u1a_ModReq "
- `E64` (shared_string): "u1l_Eprom_ModReq"
- `F64` (shared_string): "u1l_Eprom_ModReq"

### Row 65

- `D65` (number): "255"
- `E65` (number): "0"
- `F65` (number): "255"
