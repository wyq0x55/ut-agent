# PSD再構築 Ver.1.6 — 4-1 变量之间比较

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**变量之间比较**
- 原表名称："変数同士を比較する"
- 稳定 ID：`4-1`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!B130:G142`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：变量与变量比较的最小、邻接和最大值组合。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 130

- `B130` (number): "4"
- `C130` (shared_string): "条件判断文"

### Row 131

- `D131` (shared_string): "観点：期待通りに分岐が行われていることを確認する"

### Row 132

- `C132` (shared_string): "4-1"
- `D132` (shared_string): "変数同士を比較する"

### Row 134

- `D134` (shared_string): "例．If( u1_val_a == u1_val_b )"

### Row 135

- `D135` (shared_string): "入力値１"
- `E135` (shared_string): "入力値２"
- `F135` (shared_string): "説明"

### Row 136

- `D136` (shared_string): "u1_val_a"
- `E136` (shared_string): "u1_val_b"
- `F136` (shared_string): "u1_val_a"
- `G136` (shared_string): "u1_val_b"

### Row 137

- `D137` (number): "0"
- `E137` (number): "0"
- `F137` (shared_string): "最小値"
- `G137` (shared_string): "最小値"

### Row 138

- `D138` (number): "0"
- `E138` (number): "1"
- `F138` (shared_string): "最小値"
- `G138` (shared_string): "最小値+1"

### Row 139

- `D139` (number): "0"
- `E139` (number): "255"
- `F139` (shared_string): "最小値"
- `G139` (shared_string): "最大値"

### Row 140

- `D140` (number): "1"
- `E140` (number): "0"
- `F140` (shared_string): "最小値+1"
- `G140` (shared_string): "最小値"

### Row 141

- `D141` (number): "255"
- `E141` (number): "0"
- `F141` (shared_string): "最大値"
- `G141` (shared_string): "最小値"

### Row 142

- `D142` (number): "255"
- `E142` (number): "255"
- `F142` (shared_string): "最大値"
- `G142` (shared_string): "最大値"
