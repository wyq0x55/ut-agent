# ut-agent

嵌入式 C 单元测试语义生成流水线。C++ `ut-clang-extract` 产生 Typed FunctionIR v3，Python 使用版本化 TestBaseline 和项目上下文确定性生成语义测试套件，再由独立的 WinAMS target package 投影为 TestCsv、Stub、DefineVar 和 Harness。

## 当前架构

```text
C Source + CompileContext
        ↓
ut-clang-extract (C++/LibTooling)
        ↓
Typed FunctionIR v3
        ↓
ResolvedProjectContext
  ├─ versioned TestBaseline
  ├─ project-level MC/DC switch
  └─ optional approved ProjectRulePack / exceptions
        ↓
Obligation → Constraint → Solve → Evaluate → Oracle → Validate → Suite
        ↓
SemanticTestSuite
        ↓
targets/winams
        ↓
TestCsv / Stub / DefineVar / Harness
```

`generation/` 只负责源码语义驱动的确定性 Suite；`targets/winams/` 负责目标格式和执行适配。`learning/` 只用于离线 Golden/语料比较、gap 分类和候选规则推导，正常生成不读取历史 Golden。

## 目录边界

- `tooling/ut-clang-extract/`：唯一的 C 语义事实生产者。
- `src/ut_agent/ir/`：Typed FunctionIR v3 模型、JSON 编解码和 schema 校验。
- `src/ut_agent/baseline/`：版本化基础 TestBaseline。
- `src/ut_agent/project/`：ProjectManifest 和 ResolvedProjectContext。
- `src/ut_agent/generation/`：Obligation 到 SemanticTestSuite 的确定性流水线。
- `src/ut_agent/targets/winams/`：WinAMS TestCsv、Stub、DefineVar 和 Harness 投影。
- `src/ut_agent/learning/`、`reporting/`：离线比较、语料和证据报告。
- `config/projects/`：项目与 baseline/version 的唯一绑定入口。
- `docs/baselines/`：基准来源证据和人工复核输入，不等同于 runtime rule。

## CLI quick start

以下命令均来自当前 argparse CLI；生成结果放在本地 `.tmp/`，不会覆盖参考语料。

先用 uv 创建并锁定项目环境：

```bash
uv sync --extra dev
```

如果要使用本机 Scoop 中的解释器，可显式指定：
`uv sync --python "C:\Users\1068970-z461\scoop\apps\python\3.14.7\python.exe" --extra dev`。

```bash
# 解析源码并输出 FunctionIR JSON
uv run ut-agent parse <source.c> -f <function> -o .tmp/<function>.ir.json

# 使用项目 manifest 生成 WinAMS CSV（含 NEEDS_REVIEW 的部分候选）
uv run ut-agent gen <source.c> -f <function> \
  --manifest config/projects/N-O2608-PSD-087.json \
  --config-root config \
  --out .tmp/<function>

# 对项目索引中的全部函数生成并执行 Golden 语义校验
uv run ut-agent validate-corpus \
  --manifest config/projects/N-O2608-PSD-087.corpus.json \
  --out .tmp/N-O2608-PSD-087
```

`parse` 只产生 FunctionIR；`gen` 和 `validate-corpus` 会写出 CSV。Suite 为 `NEEDS_REVIEW` 时，CSV 只包含 intent-level 已验证的部分，未解决 obligation/oracle 仍保留在 `test-intents.json`，并在 manifest 中标记 `csv_kind=partial_candidate`。这类 CSV 用于 Golden/列结构对比，不代表可以直接执行。`validate-corpus` 通过 corpus manifest 间接解析项目 baseline，不在 corpus manifest 中复制 baseline；项目校验同时写出 `project-validation.json`、`project-validation.md` 和兼容用的 `corpus-validation-report.json`。

## 事实源与状态

正式 runtime baseline 位于 `config/baselines/`，例如 `psd-rebuild@1.0`；项目级 `mcdc_enabled` 位于 ProjectManifest。源码无法证明的事实保留为 `UNKNOWN`、`UNSUPPORTED` 或 `NEEDS_REVIEW`，不会用占位值伪装为有效测试。

历史 Golden 只进入 `learning`/corpus validation 闭环：

```text
Human Golden → normalize → semantic compare → gap taxonomy
             → root cause → synthetic + real regression
```

## 进一步阅读

- [文档入口](docs/README.md)
- [当前架构](docs/architecture.md)
- [项目配置](docs/project-config.md)
- [语料校验闭环](docs/corpus-validation.md)
- [WinAMS CSV contract](docs/winams/coverage-csv.md)
- [Agent 工程约束](AGENTS.md)

`examples/` 是本地/外部真实 corpus 的 gitignored 挂载约定，clone 后不保证存在；synthetic fixture 放在 `tests/fixtures/` 等正式测试目录。

## 依赖

- uv（负责 Python 环境、依赖解析和 `uv.lock`）
- Python >= 3.10（本地可用 `uv sync --python <path>` 指定解释器）
- LLVM/Clang 16+（构建 C++ `ut-clang-extract`）
- Arm GNU Toolchain（`arm-none-eabi-gcc`，仅用于独立 ARM 示例）

## License

MIT
