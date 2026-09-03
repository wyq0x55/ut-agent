# PSD再構築 Ver.1.6 — 1-2 右边是常量时的赋值

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**右边是常量时的赋值**
- 原表名称："右辺が定数の場合"
- 稳定 ID：`1-2`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!C67:F73`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：常量赋值前设置与目标值不同的初始状态。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 67

- `C67` (shared_string): "1-2"
- `D67` (shared_string): "右辺が定数の場合"

### Row 68

- `D68` (shared_string): "代入される変数には、初期値を設定しておく（初期値：代入される変数の値と異なる値を設定する）"

### Row 70

- `D70` (shared_string): "例．u2a_CntLoop = U2G_DAT_MIN ;                （※U2G_DAT_MINの値・・・0）"

### Row 71

- `D71` (shared_string): "入力値"
- `E71` (shared_string): "初期値"
- `F71` (shared_string): "期待値"

### Row 72

- `D72` (shared_string): "-"
- `E72` (shared_string): "u2a_CntLoop"
- `F72` (shared_string): "u2a_CntLoop"

### Row 73

- `D73` (shared_string): "-"
- `E73` (number): "65535"
- `F73` (number): "0"
