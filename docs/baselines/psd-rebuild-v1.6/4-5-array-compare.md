# PSD再構築 Ver.1.6 — 4-5 数组比较

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**数组比较**
- 原表名称："任意のインデックスに固定して比較を行う。(任意のインデックス以外はFALSEの値を入れる)"
- 稳定 ID：`4-5`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!C210:J221`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：固定数组索引进行比较，其他数组元素设置为 FALSE 侧，并覆盖表数组索引。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 210

- `D210` (shared_string): "配列との比較"

### Row 211

- `C211` (shared_string): "4-5"
- `D211` (shared_string): "任意のインデックスに固定して比較を行う。(任意のインデックス以外はFALSEの値を入れる)"

### Row 212

- `D212` (shared_string): "ただし、テーブルとして扱われる配列はすべてのインデックスで確認する"

### Row 214

- `D214` (shared_string): "例．If( U1_VAL <= u1_ary[ u1_index ] )            ※u1_ary[  ]のサイズは10、U1_VALの値は5とする"
- `J214` (shared_string): "U1_VAL-1 or and U1_VAL+1(※U1_VALと真偽が異なる側)"

### Row 215

- `D215` (shared_string): "入力値"
- `I215` (shared_string): "説明"

### Row 216

- `D216` (shared_string): "u1_index"
- `E216` (shared_string): "u1_aray[0]"
- `F216` (shared_string): "u1_aray[1]"
- `G216` (shared_string): "…"
- `H216` (shared_string): "u1_aray[9]"
- `I216` (shared_string): "u1_aray[0]"

### Row 217

- `D217` (number): "0"
- `E217` (number): "0"
- `F217` (number): "0"
- `G217` (number): "0"
- `H217` (number): "0"
- `I217` (shared_string): "最小値"

### Row 218

- `D218` (number): "0"
- `E218` (number): "4"
- `F218` (number): "0"
- `G218` (number): "0"
- `H218` (number): "0"
- `I218` (shared_string): "定数値-1"

### Row 219

- `D219` (number): "0"
- `E219` (number): "5"
- `F219` (number): "0"
- `G219` (number): "0"
- `H219` (number): "0"
- `I219` (shared_string): "定数値"

### Row 220

- `D220` (number): "0"
- `E220` (number): "6"
- `F220` (number): "0"
- `G220` (number): "0"
- `H220` (number): "0"
- `I220` (shared_string): "定数値+1"

### Row 221

- `D221` (number): "0"
- `E221` (number): "255"
- `F221` (number): "0"
- `G221` (number): "0"
- `H221` (number): "0"
- `I221` (shared_string): "最大値"
