# 文档入口

本目录按资料状态区分事实源，当前实现只以 Current / Normative 文档、生产代码、schema 和 runtime config 为准。

## 资料状态

| 状态 | 位置 | 用途 |
| --- | --- | --- |
| Current / Normative | `README.md`、`AGENTS.md`、`architecture.md`、`project-config.md`、`corpus-validation.md`、`winams/` | 当前实现、开发约束和验收 contract |
| Source Evidence | `baselines/`、[原始基准工作簿](単体テスト項目基準書_Ver.1.6.xlsx) | 来源转录、原始单元格和人工复核输入 |
| Historical / Archive | `archive/` | 仅用于理解历史方案和阶段报告，不是当前需求事实源 |
| Generated / Reports | `archive/reports/` 以及任务产生的 `.tmp/` | 对比、扫描和验证报告；不改变生产语义 |

`docs/archive/**` 永远不是当前需求事实源，也不是 Agent 约束来源。历史资料顶部会标注 `Historical document. Not normative.`。

## 当前文档

- [当前架构](architecture.md)
- [项目配置与版本锁](project-config.md)
- [语料校验闭环](corpus-validation.md)
- [WinAMS coverage CSV contract](winams/coverage-csv.md)
- [PSD 基准来源证据](baselines/psd-rebuild-v1.6/index.md)
- [PSD runtime baseline](../config/baselines/psd-rebuild/1.0.yaml)
- [历史资料说明](archive/README.md)
