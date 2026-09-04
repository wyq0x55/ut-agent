"""Typed FunctionIR contract shared by the extractor and Python adapters.

The standalone C++ extractor is the only producer of C semantic facts.  The
Python side may project and validate those facts, but must not recover them
from source spelling, type names, or source ranges.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import dataclasses
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
class TypeInfo:
    """Extractor-proven type and value-domain facts.

    ``kind=unknown`` is an explicit extractor result, not a permission for a
    consumer to infer a category from ``canonical_type``.
    """

    canonical_type: str = ""
    kind: str = "unknown"
    bit_width: Optional[int] = None
    signed: Optional[bool] = None
    min_value: Optional[Union[int, float]] = None
    max_value: Optional[Union[int, float]] = None
    enum_values: dict[str, int] = field(default_factory=dict)
    pointer_depth: int = 0
    pointee_type: Optional[str] = None
    pointee_info: Optional["TypeInfo"] = None
    is_const: bool = False
    is_volatile: bool = False

    @property
    def is_scalar(self) -> bool:
        return self.kind in {"integer", "enum", "bool", "float"}


@dataclass
class ValueOrigin:
    """Typed provenance for a value used by a branch or effect."""

    kind: str
    expression: str = ""
    driver: Optional[str] = None
    callee: Optional[str] = None
    call_id: Optional[str] = None
    call_offset: Optional[int] = None
    call_order: Optional[int] = None
    base: Optional[str] = None
    index: Optional[str] = None
    field: Optional[str] = None
    table_values: dict[str, int] = dataclasses.field(default_factory=dict)


@dataclass
class Effect:
    """A source-proven value effect with guards and ordering."""

    path: str = ""
    value: str = ""
    constant_value: Optional[int] = None
    source_offset: int = -1
    order: int = -1
    guards: list[dict[str, Any]] = field(default_factory=list)
    origin: Optional[ValueOrigin] = None
    name: Optional[str] = None
    operator: str = "="


@dataclass
class FieldAccess:
    path: str
    read: bool = False
    write: bool = False
    copied_from_local: bool = False
    line: int = 1
    offset: int = 0
    read_line: int = 0
    read_offset: int = 0
    write_line: int = 0
    write_offset: int = 0


@dataclass
class RecordLayoutField:
    """Extractor-proven storage facts for one record leaf field."""

    path: str
    bit_offset: int
    bit_width: int
    is_bitfield: bool = False
    storage_path: str = ""
    storage_bit_offset: int = 0
    storage_width: int = 0


@dataclass
class GlobalObject:
    name: str
    read: bool = False
    write: bool = False
    is_const: bool = False
    is_volatile: bool = False
    is_union: bool = False
    source_file: str = ""
    array_sizes: list[int] = field(default_factory=list)
    # Extractor-proven variables used as dynamic array subscripts.  This is
    # intentionally a relation on the object, not a Python guess from the
    # source spelling; consumers can intersect the bounds of objects accessed
    # through the same driver.
    index_drivers: list[str] = field(default_factory=list)
    field_paths: list[str] = field(default_factory=list)
    field_accesses: list[FieldAccess] = field(default_factory=list)
    record_layout: list[RecordLayoutField] = field(default_factory=list)
    read_line: int = 0
    read_offset: int = 0
    write_line: int = 0
    write_offset: int = 0
    provenance: Optional[Provenance] = None


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
    right: Optional[str] = None    # extractor-provided RHS for dynamic comparisons
    cond_text_spelling: str = ""
    cond_text_expanded: str = ""
    type_spelling: Optional[str] = None
    canonical_type: Optional[str] = None
    qualifiers: list[str] = field(default_factory=list)
    type_info: Optional[TypeInfo] = None
    provenance: Optional[Provenance] = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class Case:
    """switch 的一个 case；value 为 None 表示 default。"""

    label: str
    value: Optional[int]
    is_default: bool
    value_proof: Optional[str] = None
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
    parent_outcome: Optional[bool] = None
    condition_tree: Optional[dict[str, Any]] = None
    selector: Optional[ValueOrigin] = None
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
    arg_type_infos: list[Optional[TypeInfo]] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)  # 被调函数签名（stub 生成用）
    ret_type: str = "void"
    callee_kind: str = "direct"
    max_occurrences: int = 1
    return_used: bool = False
    pointer_arguments: dict[str, dict[str, bool]] = field(default_factory=dict)
    caller_param_fields: dict[str, list[str]] = field(default_factory=dict)
    caller_param_output: dict[str, bool] = field(default_factory=dict)
    param_fields: dict[str, list[str]] = field(default_factory=dict)
    return_fields: list[str] = field(default_factory=list)
    guards: list[dict[str, Any]] = field(default_factory=list)
    provenance: Optional[Provenance] = None
    extensions: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


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
    type_info: Optional[TypeInfo] = None
    value_origin: Optional[ValueOrigin] = None
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
    type_info: Optional[TypeInfo] = None
    access_paths: list[dict[str, Any]] = field(default_factory=list)
    write_effects: list[Effect] = field(default_factory=list)
    write_status: str = "unknown"
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class FunctionIR:
    name: str
    file: str
    line: int
    ret_type: str
    line_end: int = 0            # 函数体结束行（含），函数抽取窗口用
    is_static: bool = False
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
        "name": "ut-clang-extract", "version": "0.3.0", "clang_version": "unknown"
    })
    parameter_write_effects: list[Effect] = field(default_factory=list)
    global_write_effects: list[Effect] = field(default_factory=list)
    local_value_effects: list[Effect] = field(default_factory=list)
    return_effects: list[Effect] = field(default_factory=list)
    global_objects: list[GlobalObject] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        from ut_agent.ir.codec import function_ir_to_document

        return function_ir_to_document(self)
