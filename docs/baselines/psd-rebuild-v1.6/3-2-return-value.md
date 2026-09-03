# PSD再構築 Ver.1.6 — 3-2 函数返回值

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**函数返回值**
- 原表名称："関数の戻り値"
- 稳定 ID：`3-2`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!C113:F119`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：将 Stub 返回值作为可观察输入，并确认目标函数使用后的结果。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 113

- `C113` (shared_string): "3-2"
- `D113` (shared_string): "関数の戻り値"

### Row 114

- `D114` (shared_string): "スタブ関数の戻り値を変数とみなして実施する"

### Row 116

- `D116` (shared_string): "例．u1l_dat_test = u1l_job_test() ;            ※関数の戻り値 RET_u1l_job_test"

### Row 117

- `D117` (shared_string): "入力値"
- `E117` (shared_string): "初期値"
- `F117` (shared_string): "期待値"

### Row 118

- `D118` (shared_string): "RET_u1l_job_test"
- `E118` (shared_string): "u1l_dat_test"
- `F118` (shared_string): "u1l_dat_test"

### Row 119

- `D119` (number): "255"
- `E119` (number): "0"
- `F119` (number): "255"
