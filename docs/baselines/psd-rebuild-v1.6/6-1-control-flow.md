# PSD再構築 Ver.1.6 — 6-1 各种条件语句

> 本文档面向人和 AI。中文内容是使用说明；日文单元格是原始证据，语义规则仍需人工复核。

## 基准信息

- 中文基准名：**各种条件语句**
- 原表名称："色々な条件文"
- 稳定 ID：`6-1`
- 来源：`docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築` / `PSD再構築!B230:I274`
- 原文件 SHA-256：`d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- 基准类别：`生成输入基准`
- 是否参与生成：`是`
- 复核状态：`需人工复核`
- 基准目的：if/else-if、switch/default 和 for/while 的控制流、case 及循环次数。

## AI 使用边界

- 允许：只能使用已由 FunctionIR 的类型、AST 或 CFG 事实证明的内容生成候选用例。
- 禁止：不得从原文描述、函数名或猜测补出源码事实；证据不足时保持 NEEDS_REVIEW。

## 人工复核清单

- 确认该基准是否适用于当前产品和目标函数。
- 确认输入值、预期值、Stub、Oracle 和寄存器/I-O 事实都有来源。
- 确认是否存在项目例外；例外必须记录理由和来源，不直接静默放宽。

## 原始单元格证据（保留日文）

### Row 230

- `B230` (number): "6"
- `C230` (shared_string): "その他"

### Row 231

- `C231` (shared_string): "6-1"
- `D231` (shared_string): "色々な条件文"

### Row 233

- `D233` (shared_string): "条件式の種類"

### Row 235

- `D235` (shared_string): "if文"

### Row 236

- `D236` (shared_string): "4.条件判断文に従う"

### Row 238

- `D238` (shared_string): "else if文"

### Row 239

- `D239` (shared_string): "分岐元のif文と条件が重複しても、全ての条件を作成する。"

### Row 241

- `D241` (shared_string): "switch文"

### Row 242

- `D242` (shared_string): "期待通りに分岐が行われていることを確認する"

### Row 243

- `D243` (shared_string): "分岐条件の変数値を変えて、期待するcase文に移行するかを判定する。"

### Row 244

- `D244` (shared_string): "変数値の値は、各case文に移行する値、default文に移行する値、および型の最小値、最大値とする。"

### Row 246

- `D246` (shared_string): "例."
- `F246` (shared_string): "値の区分"
- `G246` (shared_string): "入力値"

### Row 247

- `D247` (shared_string): "switch( u1_val )"
- `G247` (shared_string): "u1_val"

### Row 248

- `D248` (shared_string): "{"
- `F248` (shared_string): "最小値/default"
- `G248` (number): "0"

### Row 249

- `D249` (shared_string): "    case 5:"
- `F249` (shared_string): "case 5-1"
- `G249` (number): "4"

### Row 250

- `D250` (shared_string): "        ～処理１～"
- `F250` (shared_string): "case 5"
- `G250` (number): "5"

### Row 251

- `D251` (shared_string): "    case 10:"
- `F251` (shared_string): "case 5+1"
- `G251` (number): "6"

### Row 252

- `D252` (shared_string): "        ～処理２～"
- `F252` (shared_string): "case 10-1"
- `G252` (number): "9"

### Row 253

- `D253` (shared_string): "    default :"
- `F253` (shared_string): "case 10"
- `G253` (number): "10"

### Row 254

- `D254` (shared_string): "}"
- `F254` (shared_string): "case 10+1"
- `G254` (number): "11"

### Row 255

- `F255` (shared_string): "最大値/default"
- `G255` (number): "255"

### Row 257

- `F257` (shared_string): "※case文の削除の場合は、削除された値（TRUE値）を設定し、default文が実行されることを確認"

### Row 258

- `D258` (shared_string): "for文、while文"

### Row 259

- `D259` (shared_string): "指定回数分ループを行っているかを判定する(ループ回数が期待する回数に対して過不足無いかを確認する)。"

### Row 260

- `D260` (shared_string): "winAMSで確認できない場合はシミュレータを使用して確認する。"

### Row 262

- `D262` (shared_string): "例：　※U1_IDX_MAX は5とする"

### Row 263

- `D263` (shared_string): "for(u1a_idx=0;U1_IDX_MAX > u1a_idx;u1a_idx++)"

### Row 264

- `D264` (shared_string): "{"

### Row 265

- `D265` (shared_string): "       u1_aray[u1a_idx] = U1_ZERO;"

### Row 266

- `D266` (shared_string): "       u1_index++;"

### Row 267

- `D267` (shared_string): "       stub();"

### Row 268

- `D268` (shared_string): "}"

### Row 269

- `D269` (shared_string): "初期値"

### Row 270

- `D270` (shared_string): "u1_aray[0]"
- `E270` (shared_string): "…"
- `F270` (shared_string): "u1_aray[4]"
- `G270` (shared_string): "u1_aray[5]"
- `H270` (shared_string): "u1_index"
- `I270` (shared_string): "stub_CNT"

### Row 271

- `D271` (number): "255"
- `E271` (number): "255"
- `F271` (number): "255"
- `G271` (number): "255"
- `H271` (number): "0"
- `I271` (number): "0"

### Row 272

- `D272` (shared_string): "期待値"

### Row 273

- `D273` (shared_string): "u1_aray[0]"
- `E273` (shared_string): "…"
- `F273` (shared_string): "u1_aray[4]"
- `G273` (shared_string): "u1_aray[5]"
- `H273` (shared_string): "u1_index"
- `I273` (shared_string): "stub_CNT"

### Row 274

- `D274` (number): "0"
- `E274` (number): "0"
- `F274` (number): "0"
- `G274` (number): "255"
- `H274` (number): "5"
- `I274` (number): "5"
