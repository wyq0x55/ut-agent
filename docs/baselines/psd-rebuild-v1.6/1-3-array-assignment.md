# PSD再構築 Ver.1.6 — 1-3 数组赋值

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**数组赋值**
- 原表名称："配列への代入"
- 稳定 ID：`1-3`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!C75:K82`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：固定数组索引，确认目标元素变化且其他元素保持语义不变。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 75

- `C75` (shared_string): "1-3"
- `D75` (shared_string): "配列への代入"

### Row 76

- `D76` (shared_string): "任意の配列要素の値が期待値どおりに変化しており、それ以外の配列要素の値が変化しないことを確認すること"

### Row 77

- `D77` (shared_string): "配列のインデックスを固定して実施する (期待値はすべての配列要素の値を確認すること)"

### Row 79

- `D79` (shared_string): "例．u1_ary[ u1_index ] = u1a_val_a ;            ※u1_ary[  ]のサイズは10"

### Row 80

- `D80` (shared_string): "入力値"
- `F80` (shared_string): "初期値"
- `I80` (shared_string): "期待値"

### Row 81

- `D81` (shared_string): "u1a_val_a"
- `E81` (shared_string): "u1_index"
- `F81` (shared_string): "u1_ary[ 0 ]"
- `G81` (shared_string): "…"
- `H81` (shared_string): "u1_ary[ 9 ]"
- `I81` (shared_string): "u1_ary[ 0 ]"
- `J81` (shared_string): "…"
- `K81` (shared_string): "u1_ary[ 9 ]"

### Row 82

- `D82` (number): "255"
- `E82` (number): "0"
- `F82` (number): "0"
- `G82` (number): "0"
- `H82` (number): "0"
- `I82` (number): "255"
- `J82` (number): "0"
- `K82` (number): "0"
