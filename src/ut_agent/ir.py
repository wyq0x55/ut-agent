"""IR：全流水线中间表示（M0 契约）。

消费方：flow(来源判定) / cases(边界值组合) / stub(生成) / winams(CSV 渲染)。
字段语义与 docs/用例表与CSV格式规格.md 对齐。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Union


@dataclass
class SourceLocation:
    file: str
    line: int
    column: int
    offset: int
    end_offset: int


@dataclass
class Provenance:
    spelling: SourceLocation
    expansion: SourceLocation
    macro_stack: list[str] = field(default_factory=list)
    ast_kind: str = "Unknown"


@dataclass
class Atom:
    """原子条件：不可再拆的比较。多原子经 && / || 组合成判定。"""

    var: str                       # 控制变量（成员路径含前缀，如 CanIf_Global.initRun）
    var_type: Optional[str]        # 变量类型（尽力解析）
    op: str                        # == != < <= > >=
    boundary: Optional[Union[int, float]]  # 比较边界字面值（宏/枚举展开后的真实值）
    boundary_name: Optional[str]   # 枚举名（CANIF_GET_ONLINE）；纯字面值/宏为 None
    text: str                      # 展开后的原子条件文本
    mask: Optional[int] = None     # 位掩码条件的已解析掩码（例如 x & 0x30）
    cond_text_spelling: str = ""
    cond_text_expanded: str = ""
    type_spelling: Optional[str] = None
    canonical_type: Optional[str] = None
    qualifiers: list[str] = field(default_factory=list)
    provenance: Optional[Provenance] = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class Case:
    """switch 的一个 case；value 为 None 表示 default。"""

    label: str
    value: Optional[int]
    is_default: bool
    provenance: Optional[Provenance] = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class Branch:
    """分支语句登记（CSV 的 Bxx 注释行来源）。kind ∈ if|elseif|while|dowhile|for|switch|ternary"""

    bid: str
    kind: str
    line: int
    file: Optional[str] = None
    cond_text: str = ""            # 条件原文（宏展开后）
    atoms: list[Atom] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    from_macro: Optional[str] = None   # 该语句由哪个函数宏展开而来（VALIDATE_RV 等）
    chain_index: int = 0           # else-if 链内序号，0=链头 if
    connective: Optional[str] = None  # 多原子间的连接词 "&&"|"||"（单原子 None；混合 M2 标记）
    # M2 填充：可达域（值域合法 + 前置路径可达）min/max；M1 阶段为 None
    reach_min: Optional[Union[int, float]] = None
    reach_max: Optional[Union[int, float]] = None
    # 配置/常量传播结果。None 表示源码上下文不足，不能判定恒真/恒假。
    constant_value: Optional[bool] = None
    constant_reason: Optional[str] = None
    cond_text_spelling: str = ""
    cond_text_expanded: str = ""
    parent_bid: Optional[str] = None
    provenance: Optional[Provenance] = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallSite:
    """被测函数内的一个被调用函数（stub 候选）。order 即 stub 编号 <k>（00 起，按首次出现）。"""

    order: int
    callee: str
    line: int
    via_macro: Optional[str] = None   # 经宏展开产生的调用（DET_REPORT_ERROR/VALIDATE_RV）
    ptr_call: bool = False            # 函数指针/分发表调用
    is_static: bool = False           # 被调函数为 static（stub 定义需带 static）
    table_base: Optional[str] = None  # 指针表全局名（CanIfDispatchConfig / CanIfUserTxConfirmations）
    table_member: Optional[str] = None  # 分发表成员名（回调数组为 None）
    arg_types: list[str] = field(default_factory=list)  # 调用点实参类型（表 stub 签名）
    params: list[Param] = field(default_factory=list)  # 被调函数签名（stub 生成用）
    ret_type: str = "void"
    provenance: Optional[Provenance] = None
    extensions: dict[str, Any] = field(default_factory=dict)


def is_scalar_type(t: str, enums: dict) -> bool:
    """标量类型判定（int 族 / boolean / 枚举）——ARG 列与指向物设定可否上表的依据。"""
    t = (t or "").strip()
    if not t:
        return False
    if any(k in t for k in ("uint", "sint", "int", "char", "long", "short",
                            "float", "double", "boolean", "_Bool")):
        return True
    return t in enums


@dataclass
class ControlVar:
    """控制变量登记：分支判定变量的来源分类（M2 用例表列生成的依据）。"""

    name: str                       # 列名（短名，取路径末段，如 initRun / currMode）
    var: str                        # 原子条件里的原始文本
    source: str                     # param | global | local_from_global | local | derived | stub
    set_via: Optional[str] = None   # local_from_global 时：赋值来源表达式（设定该全局）
    var_type: Optional[str] = None
    # 配置表成员等只读常量的确定值；这类变量不生成输入/IO 登录列。
    constant_value: Optional[int] = None
    constant_reason: Optional[str] = None
    branch_ids: list[str] = field(default_factory=list)
    provenance: Optional[Provenance] = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryVar:
    """源码中访问的 memory-mapped IO 宏。

    ``address`` 来自 TranslationUnit 的宏定义，``width`` 优先来自实际的
    read/write helper 调用；两者都属于源码 AST/预处理记录，不从 WinAMS
    工程文件反向猜测。
    """

    name: str
    address: int
    width: int
    read: bool = False
    write: bool = False
    # 是否在真实分支语句的作用域内访问。用于选择最小可观察寄存器集合。
    conditional: bool = False
    # 对源码中可求值的寄存器写序列生成的初始/期待值；未知时为 None。
    input_value: Optional[int] = None
    expected_value: Optional[int] = None
    provenance: Optional[Provenance] = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class Param:
    name: str
    type: str
    is_ptr: bool = False
    is_const: bool = False   # const 修饰（含指向物 const → 传入指针 PTIN）
    is_written: bool = False # 函数体是否直接写入指针指向物
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class FunctionIR:
    name: str
    file: str
    line: int
    ret_type: str
    line_end: int = 0            # 函数体结束行（含），函数抽取窗口用
    params: list[Param] = field(default_factory=list)
    globals_used: list[str] = field(default_factory=list)
    locals: list[str] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    branches: list[Branch] = field(default_factory=list)
    config: dict[str, str] = field(default_factory=dict)   # 生效配置（-D 记录）
    notes: list[str] = field(default_factory=list)         # needs_flow / 待人工标记
    enums: dict[str, dict[str, int]] = field(default_factory=dict)   # 枚举名→{成员:值}
    global_writes: list[str] = field(default_factory=list)  # 全局写回表达式文本（期待列）
    control_vars: list[ControlVar] = field(default_factory=list)     # 控制变量来源登记
    config_ptrs: list[str] = field(default_factory=list)   # 引用到的配置表指针全局（介入点）
    memory_vars: list[MemoryVar] = field(default_factory=list)  # memory-mapped IO 宏
    status: str = "OK"
    provenance: Optional[Provenance] = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    compile_context: dict[str, Any] = field(default_factory=dict)
    extractor: dict[str, str] = field(default_factory=lambda: {
        "name": "ut-agent-legacy-parser", "version": "0.1.0", "clang_version": "unknown"
    })
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        from ut_agent.parser.ir_json import function_ir_to_document

        return function_ir_to_document(self)


def selected_global_writes(ir: FunctionIR) -> list[str]:
    """返回 testcase 需要登录/期待的全局写回。

    当函数的所有分支都已由源码配置传播确定时，单纯的初始化状态写回
    不会带来路径差异；这类列会让 WinAMS 变量集合膨胀，因此不进入
    自动生成的 testcase。存在未确定分支时保留原有写回素材。
    """
    if ir.branches and all(branch.constant_value is not None for branch in ir.branches):
        return []
    return list(ir.global_writes)


def infer_branch_nesting(branches: list[Branch]) -> None:
    """Fill missing branch parents from deterministic source ranges.

    The standalone extractor preserves source spelling ranges, but an AST
    visitor can still report an ``if`` nested in a ``switch`` as a flat list.
    Choose the smallest earlier branch whose source range strictly contains
    the child. Existing explicit ``parent_bid`` values remain authoritative.
    """
    by_bid = {branch.bid: branch for branch in branches}

    def span(branch: Branch) -> tuple[int, int] | None:
        provenance = branch.provenance
        if provenance is None or provenance.spelling is None:
            return None
        start = int(provenance.spelling.offset or 0)
        end = int(provenance.spelling.end_offset or 0)
        if start <= 0 or end <= start:
            return None
        return start, end

    for child_index, child in enumerate(branches):
        if child.parent_bid is not None:
            continue
        child_span = span(child)
        if child_span is None:
            continue
        candidates: list[tuple[int, int, Branch]] = []
        for parent_index, parent in enumerate(branches[:child_index]):
            if parent.file and child.file and parent.file != child.file:
                continue
            parent_span = span(parent)
            if parent_span is None:
                continue
            if (parent_span[0] <= child_span[0]
                    and child_span[1] <= parent_span[1]
                    and parent_span != child_span):
                candidates.append((
                    parent_span[1] - parent_span[0], parent_index, parent,
                ))
        if candidates:
            _, _, parent = min(candidates, key=lambda item: (item[0], item[1]))
            if parent.bid in by_bid:
                child.parent_bid = parent.bid
