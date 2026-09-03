# WinAMS Coverage CSV Contract

本页是当前 WinAMS target projection 的人类 contract。源码语义来自 C++ `ut-clang-extract` 产生的 Typed FunctionIR；本页只定义目标文件格式、列角色、stub 约定和验证边界。

## 编码与文件结构

- CSV 使用 CP932 编码和 CRLF 换行。
- 第一行是 `mod`，第二行是 `#COMMENT`。
- `mod` 的输入/输出列数必须与实际列一致；每个数据行列数一致。
- 分支标记使用 `;$L$`、`TRUE`、`FALSE`；不可证明的分支或输出不能进入正式交付 CSV。

示例：

```text
mod,"source.c/function","function test",2,1,,,,CPP,,,"",0
#COMMENT,"input_0","input_1","return"
;$L$,if (condition)
;$L$,TRUE
,0x0,0x0,0x1
;$L$,FALSE
,0x0,0x0,0x0
```

正式 CSV 只允许包含状态为 `VALIDATED` 的语义用例。需要人工补齐的输入、oracle 或环境事实必须留在 draft/diagnostic 产物中，并标记 `NEEDS_REVIEW` 或 `UNSUPPORTED`。

## 列角色与顺序

- 被测函数标量参数使用参数名；指针地址使用 `@param`；可写指针的 caller-visible 值使用 `*param`。
- Stub 参数、返回值和调用计数使用 WinAMS 约定的 `AMSTB_...@ARGnn`、`AMSTB_...@AMIN_return[0]` 和 `CALLCNT_...` 角色。
- 全局、memory-mapped IO 和被调函数输出只能来自 FunctionIR 已证明的对象和 effect。
- 被测函数返回值存在时位于输出侧最后一列。
- 列顺序由 typed Suite/target adapter 的确定性规则决定，不能按临时变量名或历史 CSV 行猜测。

## Stub contract

每个被隔离的外部调用生成 `AMSTB_<callee>`。stub 只负责调用计数、按序记录参数、写回 AST 证明的可写指针、返回预设值：

```text
CALLCNT_<callee>
ARG<index>_<callee>[CALL_MAX]
PTROUT<index>_<callee>[CALL_MAX]
AMIN_return[CALL_MAX]
```

`CALL_MAX` 是确定性的容量配置；超过容量必须显式调大。stub 不加入业务判断。指针地址是测试夹具值，不是业务语义；generic address 不得被描述成源码规则。

## Golden 与验证

格式 Golden 只证明编码、换行、`mod/#COMMENT/$L$` 和列结构；语义 Golden 以 branch/case、边界、stub、oracle 和输入角色的语义签名比较；执行 Golden 是 WinAMS 运行产生的 XML、结果和覆盖率。三类证据不能互相冒充。

正常 `gen` 不读取历史 TestCsv 或外部预期结果补算 Oracle。Oracle 必须来自 Evaluate 后的 post-state；缺失来源就 fail closed。WinAMS 真正执行完成还必须有进程状态和输出 artifact 证据，不能只凭生成命令返回值判断。
