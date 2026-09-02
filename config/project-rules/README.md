# Versioned ProjectRulePack files

正式规则包放在 `<id>/<version>.yaml`，并使用 `project_rule_pack` 顶层对象。
只有 `status: approved` 且与项目 manifest 的 `id@version` 完全一致时，才会
进入正式生成；候选规则应留在 learning 输出目录，不能直接放入此目录。
