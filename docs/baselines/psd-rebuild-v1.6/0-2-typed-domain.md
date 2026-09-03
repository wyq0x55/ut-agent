# PSD再構築 Ver.1.6 — 0-2 各类型值的分类

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**各类型值的分类**
- 原表名称："各型に対する値の区分"
- 稳定 ID：`0-2`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!C37:H54`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：按类型复制最小值、中间值、最大值；具体宽度以 FunctionIR 类型事实为准。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 37

- `C37` (shared_string): "0-2"
- `D37` (shared_string): "各型に対する値の区分"

### Row 38

- `D38` (shared_string): "変数の値はコンパイラに依存するため注意すること"

### Row 40

- `D40` (shared_string): "例.NC30WA Ver 5.42 Release00 T1.0"

### Row 41

- `D41` (shared_string): "値の区分"
- `E41` (shared_string): "u1型変数"
- `F41` (shared_string): "u2型変数"
- `G41` (shared_string): "u4型変数"
- `H41` (shared_string): "u8型変数"

### Row 42

- `D42` (shared_string): "最小値"
- `E42` (number): "0"
- `F42` (number): "0"
- `G42` (number): "0"
- `H42` (number): "0"

### Row 43

- `D43` (shared_string): "中央値"
- `E43` (number): "128"
- `F43` (number): "32768"
- `G43` (number): "2147483648"
- `H43` (shared_string): "9223372036854775808"

### Row 44

- `D44` (shared_string): "最大値"
- `E44` (number): "255"
- `F44` (number): "65535"
- `G44` (number): "4294967295"
- `H44` (shared_string): "18446744073709551615"

### Row 46

- `D46` (shared_string): "値の区分"
- `E46` (shared_string): "s1型変数"
- `F46` (shared_string): "s2型変数"
- `G46` (shared_string): "s4型変数"
- `H46` (shared_string): "s8型変数"

### Row 47

- `D47` (shared_string): "最小値"
- `E47` (number): "-128"
- `F47` (number): "-32768"
- `G47` (number): "-2147483648"
- `H47` (shared_string): "-9223372036854775808"

### Row 48

- `D48` (shared_string): "中央値"
- `E48` (number): "0"
- `F48` (number): "0"
- `G48` (number): "0"
- `H48` (shared_string): "0"

### Row 49

- `D49` (shared_string): "最大値"
- `E49` (number): "127"
- `F49` (number): "32767"
- `G49` (number): "2147483647"
- `H49` (shared_string): "9223372036854775807"

### Row 51

- `D51` (shared_string): "値の区分"
- `E51` (shared_string): "f4型変数"
- `F51` (shared_string): "f8型変数"
- `G51` (shared_string): "enum型"

### Row 52

- `D52` (shared_string): "最小値"
- `E52` (number): "-16777215"
- `F52` (number): "-9007199254740990"
- `G52` (number): "0"

### Row 53

- `D53` (shared_string): "中央値"
- `E53` (number): "0"
- `F53` (number): "0"
- `G53` (number): "32768"

### Row 54

- `D54` (shared_string): "最大値"
- `E54` (number): "16777215"
- `F54` (number): "9007199254740990"
- `G54` (number): "65535"
