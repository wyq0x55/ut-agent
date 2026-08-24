"""IR：全流水线中间表示（M0 契约）。

消费方：flow(来源判定) / cases(边界值组合) / stub(生成) / winams(CSV 渲染)。
字段语义与 docs/用例表与CSV格式规格.md 对齐。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional, Union


@dataclass
class Atom:
    """原子条件：不可再拆的比较。多原子经 && / || 组合成判定。"""

    var: str                       # 控制变量（成员路径含前缀，如 CanIf_Global.initRun）
    var_type: Optional[str]        # 变量类型（尽力解析）
    op: str                        # == != < <= > >=
    boundary: Optional[Union[int, float]]  # 比较边界字面值（宏/枚举展开后的真实值）
    boundary_name: Optional[str]   # 枚举名（CANIF_GET_ONLINE）；纯字面值/宏为 None
    text: str                      # 展开后的原子条件文本


@dataclass
class Case:
    """switch 的一个 case；value 为 None 表示 default。"""

    label: str
    value: Optional[int]
    is_default: bool


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
    source: str                     # param | global | local_from_global | local | stub
    set_via: Optional[str] = None   # local_from_global 时：赋值来源表达式（设定该全局）
    var_type: Optional[str] = None


@dataclass
class Param:
    name: str
    type: str
    is_ptr: bool = False
    is_const: bool = False   # const 修饰（含指向物 const → 传入指针 PTIN）


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

    def to_dict(self) -> dict:
        return asdict(self)
