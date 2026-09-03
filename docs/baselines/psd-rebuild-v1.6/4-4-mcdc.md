# PSD再構築 Ver.1.6 — 4-4 条件判断中包含 AND、OR

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**条件判断中包含 AND、OR**
- 原表名称："条件判定にAND、OR条件が含まれる場合"
- 稳定 ID：`4-4`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!C179:F208`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：AND/OR 条件组合及独立条件变化；共同变量按一个输入列处理。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 179

- `C179` (shared_string): "4-4"
- `D179` (shared_string): "条件判定にAND、OR条件が含まれる場合"

### Row 180

- `D180` (shared_string): "・AND条件の場合"

### Row 181

- `D181` (shared_string): "例．If(( 条件式１ ) && ( 条件式２ ))"

### Row 182

- `D182` (shared_string): "条件式２をTrueになる条件で固定し、条件式１の入力値を組み合わせる"

### Row 183

- `D183` (shared_string): "次に、条件式１をTrueになる条件で固定し、条件式２の入力値を組み合わせる"

### Row 185

- `D185` (shared_string): "条件式１"
- `E185` (shared_string): "条件式２"

### Row 186

- `D186` (shared_string): "T"
- `E186` (shared_string): "T"
- `F186` (boolean): "1"

### Row 187

- `D187` (shared_string): "F"
- `E187` (shared_string): "T"
- `F187` (boolean): "0"

### Row 188

- `D188` (shared_string): "T"
- `E188` (shared_string): "F"
- `F188` (boolean): "0"

### Row 190

- `D190` (shared_string): "・OR条件の場合"

### Row 191

- `D191` (shared_string): "例．If(( 条件式１ ) || ( 条件式２ ))"

### Row 192

- `D192` (shared_string): "条件式２をFalseになる条件で固定し、条件式１の入力値を組み合わせる"

### Row 193

- `D193` (shared_string): "次に、条件式１をFalseになる条件で固定し、条件式２の入力値を組み合わせる"

### Row 195

- `D195` (shared_string): "条件式１"
- `E195` (shared_string): "条件式２"

### Row 196

- `D196` (boolean): "0"
- `E196` (boolean): "0"
- `F196` (boolean): "0"

### Row 197

- `D197` (boolean): "0"
- `E197` (boolean): "1"
- `F197` (boolean): "1"

### Row 198

- `D198` (boolean): "1"
- `E198` (boolean): "0"
- `F198` (boolean): "1"

### Row 200

- `D200` (shared_string): "・条件式１と条件式２に共通の変数が使われている場合"

### Row 201

- `D201` (shared_string): "if文中に同じ変数が2回以上出てきても、入力値は１つとしてまとめる。"

### Row 202

- `D202` (shared_string): "例．If(( U1_DAT1 == u1_val_a ) || ( U1_DAT2 == u1_val_a ))      ※定数U1_DAT1の値は1, U1_DAT2の値は2とする"

### Row 203

- `D203` (shared_string): "値の区分"
- `E203` (shared_string): "入力値１"

### Row 204

- `D204` (shared_string): "最小値/定数値U1_DAT1-1"
- `E204` (shared_string): "u1_val_a = 0"

### Row 205

- `D205` (shared_string): "定数値U1_DAT1/定数値U1_DAT2-1"
- `E205` (shared_string): "u1_val_a = 1"

### Row 206

- `D206` (shared_string): "定数値U1_DAT2/定数値U1_DAT1+1"
- `E206` (shared_string): "u1_val_a = 2"

### Row 207

- `D207` (shared_string): "定数値U1_DAT2+1"
- `E207` (shared_string): "u1_val_a = 3"

### Row 208

- `D208` (shared_string): "最大値"
- `E208` (shared_string): "u1_val_a = 255"
