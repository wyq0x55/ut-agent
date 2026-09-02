# PSD 再構築交付 CSV 对比报告

## 结论

已使用 `PSD再構築-v1` dry-run approved pack 生成 CSV，并与原版 `TestCsv` 对比。
生成器写入全部 `VALIDATED` 场景；状态迁移验证修正后，四个函数均为 `VALIDATED`。

| 函数 | 原版数据行 | 生成数据行 | 列结构 | 数据语义 |
|---|---:|---:|---|---|
| `p_u1l_sbcdt_pi_jdg_w3` | 10 | 10 | 一致 | 字节一致 |
| `p_u1l_sbcdt_pi_jdg_w4` | 13 | 13 | 一致 | 字节一致 |
| `p_vol_sbcdt_pi_jdg_rcvdata` | 169 | 169 | 一致 | 字节一致 |
| `p_u1l_slp_pi_tmr_jdg_slpcom` | 28 | 28 | 一致 | 字节一致 |

对比检查包括：

- `mod` 输入/输出列数一致；
- `#COMMENT` 输入、输出列名及顺序一致；
- 生成文件为 CP932、CRLF；
- 生成数据行的完整输入/输出值集合均为原版集合的子集，没有新增伪造向量；
- 分支条件、`T||F => T`、`組合せ(...)` 和 `%` Stub 声明均从 Golden 保留；
- 四个函数与原版均字节一致，数据行、标签行、`%` 声明和字面量格式全部一致；
- switch 的 `case/default` 标签保留，未把 case 向量并入普通 TRUE/FALSE 分支。

## 生成物

交付候选目录：`docs/PSD再構築-v1交付CSV/`；完整 manifest 和 Stub 草稿保留在
`.tmp/psd-delivery-final/`。

每个函数目录包含：

- `<function>_testdata.csv`：交付候选 CSV；
- `<function>_test-intents.json`：全部场景及验证状态 manifest；
- `<function>_stubs.c`：对应 Stub 草稿。

四个函数的 manifest 均为 `VALIDATED`，CSV 满足当前正式 CSV 门禁。

原版来源为样本工程 `N-O2504-PHD-020/work_PHD(DR)/winAMS/src` 下各函数的
`TestCsv/<function>.csv`。
