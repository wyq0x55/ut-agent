# PSD再構築 Ver.1.6 — 4-3 常量与变量的大小比较

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**常量与变量的大小比较**
- 原表名称："条件判定が定数と変数の大小比較"
- 稳定 ID：`4-3`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!A158:G177`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：常量与变量大小关系比较，保留使真值发生变化的邻接值。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 158

- `C158` (shared_string): "4-3"
- `D158` (shared_string): "条件判定が定数と変数の大小比較"

### Row 159

- `D159` (shared_string): "定数値（及び　定数値-1　または　定数値+1）、変数の最小・最大値の組み合わせ。"

### Row 161

- `C161` (shared_string): "①"
- `D161` (shared_string): "例．If( U1_VAL <= u1_val_a )  ※定数：5"
- `G161` (shared_string): "U1_VAL-1 or and U1_VAL+1(※U1_VALと真偽が異なる側)"

### Row 162

- `D162` (shared_string): "値の区分"
- `E162` (shared_string): "入力値１"

### Row 163

- `E163` (shared_string): "u1_val_a"

### Row 164

- `D164` (shared_string): "最小値"
- `E164` (number): "0"
- `F164` (boolean): "0"

### Row 165

- `D165` (shared_string): "定数-1"
- `E165` (number): "4"
- `F165` (boolean): "0"

### Row 166

- `D166` (shared_string): "定数"
- `E166` (number): "5"
- `F166` (boolean): "1"

### Row 167

- `D167` (shared_string): "定数+1"
- `E167` (number): "6"
- `F167` (boolean): "1"

### Row 168

- `A168` (shared_string): ";"
- `D168` (shared_string): "最大値"
- `E168` (number): "255"
- `F168` (boolean): "1"

### Row 170

- `C170` (shared_string): "②"
- `D170` (shared_string): "例．If( U1_VAL < u1_val_a )  ※定数：5"
- `G170` (shared_string): "U1_VAL-1 or and U1_VAL+1(※U1_VALと真偽が異なる側)"

### Row 171

- `D171` (shared_string): "値の区分"
- `E171` (shared_string): "入力値１"

### Row 172

- `E172` (shared_string): "u1_val_a"

### Row 173

- `D173` (shared_string): "最小値"
- `E173` (number): "0"
- `F173` (boolean): "0"

### Row 174

- `D174` (shared_string): "定数-1"
- `E174` (number): "4"
- `F174` (boolean): "0"

### Row 175

- `D175` (shared_string): "定数"
- `E175` (number): "5"
- `F175` (boolean): "0"

### Row 176

- `D176` (shared_string): "定数+1"
- `E176` (number): "6"
- `F176` (boolean): "1"

### Row 177

- `A177` (shared_string): ";"
- `D177` (shared_string): "最大値"
- `E177` (number): "255"
- `F177` (boolean): "1"
