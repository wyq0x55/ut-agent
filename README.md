# ut-agent

嵌入式 C 单元测试用例自动生成流水线。确定性枚举为主体（~90%），LLM 仅在三个介入点兜底。

## 架构

```
C 源码 + 配置头
  │
  ▼
┌─────────────────────────────────────┐
│  parser/  libclang 解析 → FunctionIR │
├─────────────────────────────────────┤
│  flow/     控制流 → 原子条件 + 控制变量 │
├─────────────────────────────────────┤
│  cases/    边界值枚举 + 组合去冗余     │
├─────────────────────────────────────┤
│  stub/     WinAMS AMSTB stub（CALLCNT/ARG/PTROUT）│
├─────────────────────────────────────┤
│  host/     harness + ARM GCC 编译执行       │
├─────────────────────────────────────┤
│  winams/   WinAMS TestCsv（mod/#COMMENT）   │
├─────────────────────────────────────┤
│  llm/      LLM 兜底（来源判定/有状态stub/覆盖率闭环）│
└─────────────────────────────────────┘
```

## 批量验证结果

| 模块 | 函数数 | 全自动 | 说明 |
|------|--------|--------|------|
| CanIf | 12 | **11** | 仅剩双重指针形参 1 个介入点 |
| PduR | 29 | **27** | 2 个深指针 segfault（三层配置依赖） |

接入新模块只需：
1. 新建 `examples/configs/<module>/` 最小配置头（一次性成本）
2. 框架本体零改动

## 快速开始

```bash
# 安装（需要系统安装 libclang 16+）
pip install -e ".[dev]"

# 批量跑某个源文件
ut-agent batch <source.c> -D MACRO=val --out .build/batch/<name> -I <include> ...

# 生成 WinAMS 原生 stub 与 TestCsv（CSV 写为 CP932/CRLF）
ut-agent gen <source.c> -f <function> --out .build/winams/<function> \
  -I <include> --reference-csv <参考 TestCsv>

# 用 Arm GNU Toolchain 生成带 DWARF 的 ARM ELF
ut-agent arm-build <source.c> -o .build/winams/<function>.elf \
  --entry <function> -I <include>

# 在 WinAMS 安装了 armgccomf.EXE 时，同时生成可导入的 .xlo
ut-agent arm-build <source.c> <function>_stubs.c \
  -o .build/winams/<function>.out --omf-output .build/winams/<function>.xlo \
  --entry <function> -I <include>

# 回归测试（需要外部 Classic Platform 基准源码）
pytest tests/
```

### 测试基准源码

仓库不包含体积较大的 `examples/classic-platform/` 基准源码。运行完整的
CanIf golden/host 回归前，请将基准源码放置到该目录，或设置环境变量：

```bash
export UT_AGENT_CLASSIC_PLATFORM=/path/to/classic-platform
pytest tests/
```

如果基准源码未准备，相关测试会明确跳过，不会伪装成通过；解析、枚举和
跨平台运行器的独立回归测试仍会执行。

## 铁律

1. **确定性核心**：同输入必须同输出——审查一致性的根基
2. **LLM 仅三个介入点**：来源判定兜底、有状态 stub、覆盖率闭环
3. **golden 是契约**：golden 期望值改动必须在提交说明中写明理由
4. **不引入重量级框架**：裸 Python + dataclass + 纯函数

详见 `AGENTS.md` 和 `docs/用例表与CSV格式规格.md`。

## 依赖

- Python >= 3.10
- libclang >= 16
- Arm GNU Toolchain（`arm-none-eabi-gcc`，生成 WinAMS 使用的 ARM ELF）
- GCC（可选，仅用于 host 回放模式）

## License

MIT
