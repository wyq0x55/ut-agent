# PSD再構築 Ver.1.6 — 测试基准索引

> 本索引只收录当前 Issue #6 的 PSD再構築 基准。每个 section 单独保存为 Markdown/YAML，保存在 `docs/baselines/psd-rebuild-v1.6/` 中。未列入的内容不参与当前语义规则映射。

- Source: `docs/単体テスト項目基準書_Ver.1.6.xlsx` / `PSD再構築`
- SHA-256: `d59762905c0566707ecf029ab188a8a05d13e2647c3866e383e83269655aba69`
- Sections: 16
- Overall review status: `needs_review`

## Included test baselines

| ID | Title | Role | Generation | Review | Location |
| --- | --- | --- | --- | --- | --- |
| `0-1` | "評価観点" | `validation_gate` | `false` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/0-1-evaluation.md` (.yaml) |
| `0-2` | "各型に対する値の区分" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/0-2-typed-domain.md` (.yaml) |
| `1-1` | "右辺、左辺とも変数の場合" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/1-1-variable-assignment.md` (.yaml) |
| `1-2` | "右辺が定数の場合" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/1-2-constant-assignment.md` (.yaml) |
| `1-3` | "配列への代入" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/1-3-array-assignment.md` (.yaml) |
| `1-4` | "レジスタ、I/Oポートへの値設定" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/1-4-register-io-assignment.md` (.yaml) |
| `3-1` | "関数コール" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/3-1-call-count.md` (.yaml) |
| `3-2` | "関数の戻り値" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/3-2-return-value.md` (.yaml) |
| `3-3` | "関数の引数" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/3-3-arguments.md` (.yaml) |
| `4-1` | "変数同士を比較する" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/4-1-variable-compare.md` (.yaml) |
| `4-2` | "変数値と定数値の一致" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/4-2-equality.md` (.yaml) |
| `4-3` | "条件判定が定数と変数の大小比較" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/4-3-relational.md` (.yaml) |
| `4-4` | "条件判定にAND、OR条件が含まれる場合" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/4-4-mcdc.md` (.yaml) |
| `4-5` | "任意のインデックスに固定して比較を行う。(任意のインデックス以外はFALSEの値を入れる)" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/4-5-array-compare.md` (.yaml) |
| `6-1` | "色々な条件文" | `generation_input` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/6-1-control-flow.md` (.yaml) |
| `6-2` | "テストパターンの並べ方" | `ordering_policy` | `true` | `needs_review` | `docs/baselines/psd-rebuild-v1.6/6-2-ordering.md` (.yaml) |

## Excluded material

- `2` `PSD再構築!B93:C93`: 原表明确标记为演算式（テストしない）。 Disposition: `excluded`.
- `5-1` `PSD再構築!C223:D228`: asm 文在 WinAMS 中不能直接执行，原表要求使用 simulator。 Disposition: `excluded_from_winams_csv`.
- `bookkeeping` `PSD再構築!A1:S28`: 目录、封面和导航信息，不是测试输入基准。 Disposition: `excluded`.
