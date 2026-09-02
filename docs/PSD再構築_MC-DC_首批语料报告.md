# PSD 再構築 + MC/DC 首批语料报告

## 范围

- 基准 Profile：`PSD再構築`
- Profile 版本：`PSD再構築-v1`（暂定）
- MC/DC：启用；它是独立于基准 Profile 的可选维度
- 样本工程：`N-O2504-PHD-020/work_PHD(DR)`
- 本报告只使用历史 `TestCsv` 做候选规则证据，不把候选规则自动用于正式 CSV

## 已完成采集

通过 `rules collect` 对 `winAMS/src` 下首批四个函数进行源码与人工 TestCsv 配对。四个函数是规则学习证据，不是交付对象：

| 函数 | 历史场景 | 源码分支/原子 | 结果 |
|---|---:|---:|---|
| `p_u1l_sbcdt_pi_jdg_w3` | 10 | 2 / 4 | 已形成候选规则 |
| `p_u1l_sbcdt_pi_jdg_w4` | 13 | 2 / 6 | 已形成候选规则 |
| `p_vol_sbcdt_pi_jdg_rcvdata` | 169 | 14 / 13 | 已形成候选规则 |
| `p_u1l_slp_pi_tmr_jdg_slpcom` | 28 | 8 / 6 | 已形成候选规则 |

合计 210 个历史场景，候选包包含 4 个函数级场景规则、4 个源码语义模式，以及 4 个 Profile 级可复用规则；其中 `rcvdata` 的 15 个 switch 组合保留为 `kind=case`。候选包带有 `base_profile`、`profile_version`、`mcdc_enabled` 和 `approved_exceptions` 元数据，并标记 `samples_are_evidence_only=true`。`w3/w4` 的 `T||F`、`F||T`、`F||F` 和 `組合せ(...)` 标签已经按同一分支的 MC/DC 组合解析。

每个函数级候选规则还附带 `rule_evidence`：观察到的标签数、MC/DC 组合标签、case 标签和各输入列的值类别。这些摘要才是后续审批的规则证据；具体场景行仅用于复现和验证。

本批已抽出四条 Profile 级候选规则：

- `profile.PSD再構築-v1.mcdc`：对 `||` 原子执行独立变化组合；
- `profile.PSD再構築-v1.switch-case`：保留 case/default 语义；
- `profile.PSD再構築-v1.stub-contract`：保留 CALLCNT、ARG 和 AMIN_return 契约列；
- `profile.PSD再構築-v1.boundary-values`：使用 zero/one/literal/type-max 等值类别。

## 正式交付门禁

当前仍为 `candidate`，不能直接交付。模拟审批后验证结果如下：

1. `w3/w4` 的数组控制变量已经按宏展开下标映射到 WinAMS 全限定列（例如 `state[3]`、`state[4]`）；该映射仍需纳入正式审批证据。
2. `w4` 的局部控制变量 `u1a_dat_sups_rslt` 已绑定到唯一的 `AMIN_return` 列，相关 CALLCNT 也已建立别名。
3. `rcvdata` 的 `u1a_dat_vs_uv`、`u1a_dat_mtract`、`u1a_flg_cond`、配置表成员和动态数组计数已建立确定性绑定；switch 组合已保留 case 语义。动态比较在 Stub 调用后使用 Golden 记录的更新状态，169 个历史场景全部通过。
4. `slp` 的 `u1a_dat_diag_jdg`、各 Stub CALLCNT 已建立绑定，B01/B02 源码宏原子已恢复；28 个场景全部通过。
5. 非 void 返回值必须以历史 CSV 的输出列作为 Oracle；禁止用默认 `0x0` 补齐。

此前机器分类出的 30 个场景（B07/B10/B13/B14）被确认是状态时序误判：源码先执行
`l_u1g_cal_inc_grd` 更新计数，再比较 `u1_jdg_cnt`。修正为使用 Golden 的更新后
状态后，这 30 个场景均恢复为 `VALIDATED`。

以仅批准四条函数级候选规则的 dry-run 验证结果看：`w3` 10/10、`w4` 13/13、
`rcvdata` 169/169、`slp` 28/28 均为 `VALIDATED`。该 dry-run 不代表正式批准。

因此本批已生成与 Golden 字节一致的交付候选 CSV；正式交付仍需项目审批记录。

## 下一步方案

1. 将本批已实现的数组、结构体配置表成员、源码推导局部量、Stub 返回值、调用后状态和 CALLCNT 绑定纳入规则证据。
2. 由项目审批形成 PSD Profile 版本和例外记录，审批通过后把对应候选规则复制为版本化 approved pack。
3. 用 approved pack 生成 CSV；格式 Golden 仅校验基准书要求，历史 TestCsv 只作为语义/执行证据。
4. 在 WinAMS 中执行生成 CSV，比较输出 Golden，并读取覆盖率 XML 确认 PSD 分支和 MC/DC 达标。
5. 留出函数级 holdout 样本做回归，确认规则不是只记忆这四个函数。

相关规格见 [winams_coverage_csv_engine_prd.md](winams_coverage_csv_engine_prd.md)。
