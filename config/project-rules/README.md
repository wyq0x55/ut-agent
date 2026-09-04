# Versioned ProjectRulePack files

正式规则包放在 `<id>/<version>.yaml`，并使用 `project_rule_pack` 顶层对象。
只有 `status: approved` 且与项目 manifest 的 `id@version` 完全一致时，才会
进入正式生成；候选规则应留在 learning 输出目录，不能直接放入此目录。

通用语义规则（`semantic_family` 或 `semantic_pattern`）还必须满足：

- `scope.function` 为 `*`，不能绑定单个函数；
- 至少有两个项目的证据；
- `validation.strategy` 为 `leave-one-project-out`；
- `validation.status` 为 `PASS`，且每个 fold 都为 `PASS`。

函数级 `scenario_matrix` 只能作为 Golden/replay 证据，不能冒充跨项目通用规则。
