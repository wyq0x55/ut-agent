# PSD再構築 Ver.1.6 — 3-1 函数调用

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**函数调用**
- 原表名称："関数コール"
- 稳定 ID：`3-1`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!B95:E111`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：确认外部函数调用次数和 Stub 内调用计数。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 95

- `B95` (number): "3"
- `C95` (shared_string): "関数コール"

### Row 96

- `D96` (shared_string): "観点：期待通りに関数が呼ばれていること、関数入出力が正しく行われていることを確認する"

### Row 97

- `C97` (shared_string): "3-1"
- `D97` (shared_string): "関数コール"

### Row 98

- `D98` (shared_string): "スタブ関数内のインクリメント変数がインクリメントされていることを確認する"

### Row 100

- `D100` (shared_string): "test()"

### Row 102

- `D102` (shared_string): "STB関数"

### Row 103

- `D103` (shared_string): "void STB_test(void)"

### Row 104

- `D104` (shared_string): "{"

### Row 105

- `D105` (shared_string): "  CALL_test++;"

### Row 106

- `D106` (shared_string): "}"

### Row 109

- `D109` (shared_string): "入力値１"
- `E109` (shared_string): "期待値"

### Row 110

- `D110` (shared_string): "CALL_test"
- `E110` (shared_string): "CALL_test"

### Row 111

- `D111` (number): "0"
- `E111` (number): "1"
