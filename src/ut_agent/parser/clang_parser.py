"""M1 解析层：libclang 解析 C 源码 → FunctionIR。

输入三件套：源码文件 + include 路径 + 配置（-D 宏 / 强制包含的配置头）。
职责：函数抽取、参数/返回类型、全局引用、调用集（stub 候选）、
六类分支语句（if/elseif/while/dowhile/for/switch/ternary）、原子条件拆解
（枚举/宏展开后的真实边界值）、函数宏来源标记（VALIDATE_RV 等）。

不做（留给 M2 flow）：变量来源判定、可达域分析。相关疑难点写入 notes。
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Optional

from clang import cindex

from ut_agent.flow import assign
from ut_agent.ir import (
    Atom, Branch, CallSite, Case, ControlVar, FunctionIR, MemoryVar, Param,
)

# 常见“校验类”函数宏：展开后含 if/return/调用，需要在 IR 里标来源
WATCH_MACROS_DEFAULT = (
    "VALIDATE_RV",
    "VALIDATE_NO_RV",
    "DET_REPORT_ERROR",
    "DET_REPORTERROR",
    "VALIDATE",
)

COMPARE_OPS = {"==", "!=", "<", "<=", ">", ">="}
LOGIC_OPS = {"&&", "||"}

# 常见常量宏真值（配置头/项目可扩充）；用于宏展开后 IntegerLiteral 的拼写仍是宏名的情况
KNOWN_CONSTS = {
    "TRUE": 1, "FALSE": 0, "E_OK": 0, "E_NOT_OK": 1,
    "STD_ON": 1, "STD_OFF": 0, "NULL": 0,
}
CAST_KINDS = {
    cindex.CursorKind.UNEXPOSED_EXPR,   # 隐式转换在 libclang 游标中多为 UNEXPOSED
    cindex.CursorKind.CSTYLE_CAST_EXPR,
    cindex.CursorKind.PAREN_EXPR,
}


def _ensure_library() -> None:
    """pip 装的 libclang 自带动态库；个别版本需要显式指定。"""
    if cindex.Config.loaded:
        return
    try:
        cindex.Index.create()  # 能建即已配置
        return
    except Exception:
        import glob
        import sysconfig

        candidates = []
        for pat in ("**/libclang.dll", "**/libclang.so*", "**/libclang.dylib"):
            site = Path(sysconfig.get_paths()["purelib"])
            candidates += glob.glob(str(site / "clang" / "native" / pat), recursive=True)
        if candidates:
            cindex.Config.set_library_file(sorted(candidates)[0])


def parse_tu(source: Path, include_dirs=(), defines: Optional[dict] = None,
             force_include: Optional[Path] = None,
             strict: bool = True) -> cindex.TranslationUnit:
    """解析 TU。strict=True 时任何错误抛异常；strict=False 时错误挂到
    tu._ut_agent_errors，由调用方按函数范围甄别（函数级错误容忍：
    目标是通用框架，文件内其他函数的依赖问题不应阻塞被测函数抽取）。"""
    _ensure_library()
    args = ["-x", "c", "-std=c99"]
    for d in include_dirs:
        args += ["-I", str(d)]
    if force_include is not None:
        args += ["-include", str(force_include)]
    for k, v in (defines or {}).items():
        args.append(f"-D{k}={v}" if v != "" else f"-D{k}")
    index = cindex.Index.create()
    tu = index.parse(
        str(source), args=args,
        options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    errors = [d for d in tu.diagnostics
              if d.severity in (cindex.Diagnostic.Error, cindex.Diagnostic.Fatal)]
    tu._ut_agent_errors = errors
    if errors and strict:
        msgs = []
        for d in errors[:8]:
            loc = ""
            try:
                loc = f"{Path(d.location.file).name}:{d.location.line}:{d.location.column}"
            except Exception:
                pass
            msgs.append(f"  [{loc}] {d.spelling}")
        raise RuntimeError(f"{source} 解析失败（{len(errors)} 个错误）:\n" + "\n".join(msgs))
    return tu


# ---------------------------------------------------------------- 辅助

def _safe_token_spelling(token) -> str:
    """读取 token 文本；老项目的 CP932 注释可能无法被 libclang 解码为 UTF-8。

    注释不参与表达式/赋值分析，因此遇到这类 token 时跳过即可。对非注释
    token 保留空串兜底，避免单个坏 token 阻塞整个函数的确定性解析。
    """
    if token.kind == cindex.TokenKind.COMMENT:
        return ""
    try:
        return token.spelling
    except UnicodeDecodeError:
        return ""


def _token_spellings(tokens) -> list[str]:
    return [text for token in tokens
            if (text := _safe_token_spelling(token))]


def _tokens_text(tu, cur) -> str:
    return " ".join(_token_spellings(tu.get_tokens(extent=cur.extent)))


def _tokens_text_obj(cur) -> str:
    return " ".join(_token_spellings(cur.get_tokens()))


def _strip_wrappers(cur):
    while cur.kind in CAST_KINDS:
        kids = list(cur.get_children())
        if not kids:
            return cur
        cur = kids[0]
    return cur


def _strip_value_wrappers(cur):
    """去掉表达式转换包装，并跳过 CStyleCast 的 TYPE_REF 子节点。"""
    while cur.kind in CAST_KINDS:
        kids = [child for child in cur.get_children()
                if child.kind != cindex.CursorKind.TYPE_REF]
        if not kids:
            return cur
        cur = kids[-1]
    return cur


def _children(cur):
    return list(cur.get_children())


def _scan_depth0(toks) -> Optional[str]:
    """括号深度 0 的最外层算符；逻辑（&& ||）优先于比较——与 C 优先级一致方向。"""
    for target in (LOGIC_OPS, COMPARE_OPS):
        depth = 0
        for s in toks:
            if s == "(":
                depth += 1
            elif s == ")":
                depth -= 1
            elif depth == 0 and s in target:
                return s
    return None


def _operator_of(tu, parent, hint=None) -> Optional[str]:
    """从父表达式 token 序列取最外层算符。

    宏展开后子节点 extent 会坍缩、部分节点 token 为空——此时用最近一次
    拿到的非空 token 序列（hint，通常是外层括号的实参文本）兜底。
    顺序：原序列扫深度 0 → 剥一层外括号再扫（覆盖 (A==B) 整体包裹的形态）。"""
    toks = _token_spellings(tu.get_tokens(extent=parent.extent))
    if not toks and hint is not None:
        toks = list(hint)
    for _ in range(2):
        found = _scan_depth0(toks)
        if found:
            return found
        if toks and toks[0] == "(" and toks[-1] == ")":
            toks = toks[1:-1]
        else:
            break
    return None


def _value_of(cur) -> tuple[Optional[int], Optional[str]]:
    """字面值/枚举 → (真实值, 枚举名)。宏展开后的 TRUE/E_OK 已是 IntegerLiteral。"""
    cur = _strip_value_wrappers(cur)
    kids = _children(cur)
    if cur.kind == cindex.CursorKind.INTEGER_LITERAL:
        text = _tokens_text_obj(cur)
        value = _integer_token(text)
        if value is not None:
            return value, None
        # 宏展开后字面量拼写仍为宏名（TRUE/E_OK 等）
        if text in KNOWN_CONSTS:
            return KNOWN_CONSTS[text], text
        return None, text
    if cur.kind == cindex.CursorKind.FLOATING_LITERAL:
        return float(_tokens_text_obj(cur)), None
    if cur.kind == cindex.CursorKind.UNARY_OPERATOR and kids:
        text = _tokens_text_obj(cur).replace(" ", "")
        v, name = _value_of(kids[0])
        if text.startswith("-") and v is not None:
            return -v, name
        return v, name
    if cur.kind == cindex.CursorKind.DECL_REF_EXPR:
        ref = cur.referenced
        if ref is not None and ref.kind == cindex.CursorKind.ENUM_CONSTANT_DECL:
            return ref.enum_value, ref.spelling
        return None, None  # 变量对变量比较，边界留给 flow
    if cur.kind == cindex.CursorKind.CHARACTER_LITERAL:
        return int(_tokens_text_obj(cur).strip("'")), None
    return None, None


def _atom_of(tu, bin_cur, hint=None) -> Optional[Atom]:
    ch = _children(bin_cur)
    if len(ch) != 2:
        return None
    lhs, rhs = ch
    op = _operator_of(tu, bin_cur, hint)
    if op not in COMPARE_OPS:
        return None

    def is_var_side(c) -> bool:
        c = _strip_wrappers(c)
        if c.kind == cindex.CursorKind.DECL_REF_EXPR:
            return c.referenced is not None and c.referenced.kind in (
                cindex.CursorKind.VAR_DECL, cindex.CursorKind.PARM_DECL,
                cindex.CursorKind.FIELD_DECL)
        return any(is_var_side(k) for k in _children(c))

    if not is_var_side(lhs):
        lhs, rhs = rhs, lhs
        op = {"==": "==", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}[op]
    boundary, bname = _value_of(rhs)
    var_text = _tokens_text(tu, lhs)
    if not var_text and hint is not None:
        var_text = " ".join(hint)
    var_type = None
    try:
        var_type = lhs.type.spelling          # 表达式类型：成员表达式给字段类型
    except Exception:
        pass
    if var_type in (None, ""):
        ref = _find_var_decl(lhs)
        if ref is not None:
            try:
                var_type = ref.type.spelling
            except Exception:
                var_type = None
    text = _tokens_text(tu, bin_cur)
    if not text and hint is not None:
        text = " ".join(hint)
    return Atom(var=var_text, var_type=var_type, op=op,
                boundary=boundary, boundary_name=bname, text=text)


def _find_var_decl(cur):
    cur = _strip_wrappers(cur)
    if cur.kind == cindex.CursorKind.DECL_REF_EXPR and cur.referenced is not None \
            and cur.referenced.kind in (cindex.CursorKind.VAR_DECL, cindex.CursorKind.PARM_DECL):
        return cur.referenced
    for k in _children(cur):
        r = _find_var_decl(k)
        if r is not None:
            return r
    return None


def _collect_atoms(tu, cur, out: list, hint=None) -> None:
    # 逐层解包裹并同时刷新 hint：宏坍缩下内层节点 token 可能为空，
    # 外层（尤其是实参括号）的 token 序列是唯一可靠的算符文本来源
    while True:
        toks = _token_spellings(tu.get_tokens(extent=cur.extent))
        if toks:
            hint = toks
        if cur.kind in CAST_KINDS:
            kids = _children(cur)
            if len(kids) == 1:
                cur = kids[0]
                continue
        break
    if cur.kind == cindex.CursorKind.BINARY_OPERATOR:
        ch = _children(cur)
        if len(ch) == 2:
            op = _operator_of(tu, cur, hint)
            if op in LOGIC_OPS:
                _collect_atoms(tu, ch[0], out, hint)
                _collect_atoms(tu, ch[1], out, hint)
                return
            if op in COMPARE_OPS:
                a = _atom_of(tu, cur, hint)
                if a is not None:
                    out.append(a)
                return
    if cur.kind == cindex.CursorKind.UNARY_OPERATOR:  # !(...) 等：往下找比较
        for k in _children(cur):
            _collect_atoms(tu, k, out, hint)
        return
    for k in _children(cur):   # 逗号/赋值包裹等罕见形态，尽力下钻
        _collect_atoms(tu, k, out, hint)


# ---------------------------------------------------------------- 宏信息

def _macro_lines(tu, watch: tuple) -> dict:
    """{macro_name: {使用行号集合}}。来自预处理记录的宏实例化游标；
    宏展开出的语句 location 指向使用行，按行号归属最稳（extent 会坍缩，不可用）。"""
    macro_kind = getattr(cindex.CursorKind, "MACRO_INSTANTIATION", None) \
        or getattr(cindex.CursorKind, "MACRO_EXPANSION")
    result: dict = {}
    for cur in tu.cursor.walk_preorder():
        if cur.kind == macro_kind and cur.spelling in watch:
            result.setdefault(cur.spelling, set()).add(cur.location.line)
    return result


def _macro_at_line(line: int, macro_lines: dict) -> Optional[str]:
    for name, lines in macro_lines.items():
        if line in lines:
            return name
    return None


_C_INTEGER = re.compile(r"^(?:0[xX][0-9A-Fa-f]+|[0-9]+)[uUlL]*$")
_MEMORY_ACCESS = re.compile(
    r"(?:read|orwrite|andwrite|xorwrite|write).*?reg(8|16|32|64)$",
    re.IGNORECASE,
)
_MEMORY_PREFIX = re.compile(r"^U(1|2|4|8)L_")


def _memory_name_for_width(name: str, width: int) -> str:
    """按 WinAMS IO 登录宽度规范化寄存器符号前缀。

    源码中的宏前缀有时表示寄存器原始声明宽度，而 WinAMS 登录名表示
    本次访问宽度。比如 ``U4L_DMA_REG_ICDMA04`` 经过 16 位 helper 访问
    时，TestCsv/DefineVar 应使用 ``U2L_DMA_REG_ICDMA04``。
    """
    if width not in (1, 2, 4, 8):
        return name
    return _MEMORY_PREFIX.sub(f"U{width}L_", name, count=1)


def _integer_token(text: str) -> Optional[int]:
    """把 C 整数字面量（含 UL/ULL 后缀）解析为 Python 整数。"""
    if not _C_INTEGER.fullmatch(text):
        return None
    value = text.rstrip("uUlL")
    try:
        return int(value, 0)
    except ValueError:
        return None


def _macro_constant_definitions(tu) -> dict[str, int]:
    """收集 TU（含 include 头）中可解析为整数的宏定义。

    libclang 的预处理记录同时保留 MACRO_DEFINITION 和
    MACRO_INSTANTIATION。地址宏通常形如 ``((u4)0xFFFFB080UL)``，因此
    取定义体中的整数字面量即可；函数宏和普通配置表达式不会进入结果。
    """
    macro_kind = getattr(cindex.CursorKind, "MACRO_DEFINITION")
    raw: dict[str, list[str]] = {}
    for cur in tu.cursor.walk_preorder():
        if cur.kind != macro_kind:
            continue
        tokens = _token_spellings(cur.get_tokens())
        if len(tokens) < 2:
            continue
        raw[cur.spelling] = tokens[1:]

    result: dict[str, int] = {}

    def resolve(name: str, stack: set[str]) -> Optional[int]:
        if name in result:
            return result[name]
        if name in stack or name not in raw:
            return None
        tokens = list(raw[name])
        while len(tokens) >= 2 and tokens[0] == "(" and tokens[-1] == ")":
            tokens = tokens[1:-1]
        if len(tokens) == 1:
            value = _integer_token(tokens[0])
            if value is None:
                value = KNOWN_CONSTS.get(tokens[0])
            if value is None:
                value = resolve(tokens[0], {*stack, name})
            if value is not None:
                result[name] = value
            return value
        values = [value for token in tokens
                  if (value := _integer_token(token)) is not None]
        if len(values) == 1:
            result[name] = values[0]
            return values[0]
        return None

    for name in sorted(raw):
        resolve(name, set())
    return result


def _macro_name_at_cursor(cur) -> Optional[str]:
    """从宏展开 AST 节点的位置取源代码中的 object-like 宏名。"""
    try:
        file = cur.location.file
        line_no = cur.location.line
        column = cur.location.column
        if file is None or line_no <= 0:
            return None
        line = Path(file.name).read_bytes().decode("cp932", errors="replace").splitlines()[line_no - 1]
    except (OSError, IndexError, UnicodeError):
        return None
    position = max(column - 1, 0)
    matches = list(re.finditer(r"[A-Za-z_]\w*", line))
    for match in matches:
        if match.start() <= position < match.end():
            return match.group(0)
    return None


def _call_argument_value(tu, cur, index: int, definitions: dict[str, int]) -> Optional[int]:
    """解析 helper 实参中的字面量或 object-like 常量宏。"""
    args = _children(cur)[1:]
    if index >= len(args):
        return None
    argument = args[index]
    value, _ = _value_of(argument)
    if value is not None:
        return value
    for node in argument.walk_preorder():
        name = _macro_name_at_cursor(node)
        if name in definitions:
            return definitions[name]
    return None


def _derive_memory_values(ops: list[tuple[str, Optional[int]]], width: int):
    """从源码可求值的寄存器写序列生成一个有意义的初始/期待值。

    对 ``OR``/``AND`` 序列，初值取能同时触发首个置位和清位效果的值；
    对普通 write，初值取 0。该策略只使用源码 helper 的实际常量，不从
    原 TestCsv 复制手工向量。
    """
    if not ops or any(value is None for _, value in ops):
        return 0, None
    mask = (1 << (width * 8)) - 1
    first_kind, first_value = ops[0]
    assert first_value is not None
    if first_kind == "orwrite" and any(kind == "andwrite" for kind, _ in ops[1:]):
        initial = mask ^ first_value
    elif first_kind == "andwrite":
        initial = mask ^ first_value
    else:
        initial = 0
    current = initial
    for kind, value in ops:
        assert value is not None
        if kind == "write":
            current = value
        elif kind == "orwrite":
            current |= value
        elif kind == "andwrite":
            current &= value
        elif kind == "xorwrite":
            current ^= value
        current &= mask
    return initial, current


def _memory_vars(tu, target, body) -> list[MemoryVar]:
    """从目标函数的宏实例化和寄存器 helper 调用提取 memory-mapped IO。

    只登记目标函数实际使用的整数地址宏。访问宽度优先取
    ``*_read_reg16``/``*_write_reg32`` 等源码调用名，无法识别时才使用
    宏名的 ``U1L/U2L/U4L/U8L`` 前缀。这样 ``DefineVar.dat`` 的地址和
    WinAMS 宽度都由源码解析结果产生，不依赖原工程副文件。
    """
    definitions = _macro_constant_definitions(tu)
    macro_kind = getattr(cindex.CursorKind, "MACRO_INSTANTIATION")
    macro_names: list[str] = []
    for cur in tu.cursor.walk_preorder():
        if cur.kind != macro_kind or cur.spelling not in definitions:
            continue
        if not (target.location.line <= cur.location.line <= target.extent.end.line):
            continue
        if cur.spelling not in macro_names:
            macro_names.append(cur.spelling)
    if not macro_names:
        return []

    # 先记录真实分支语句的范围。无条件初始化寄存器很多，但它们不能
    # 帮助测试用例区分路径；优先从条件作用域内的访问中选代表变量。
    branch_ranges = []
    for node in body.walk_preorder():
        if node.kind in (
            cindex.CursorKind.IF_STMT,
            cindex.CursorKind.SWITCH_STMT,
            cindex.CursorKind.CONDITIONAL_OPERATOR,
        ):
            branch_ranges.append((node.extent.start.line, node.extent.end.line))

    def in_branch(line: int) -> bool:
        return any(start <= line <= end for start, end in branch_ranges)

    state: dict[str, dict] = {}
    ordered_names: list[str] = []

    def add_name(name: str) -> dict:
        if name not in state:
            state[name] = {
                "address": definitions[name], "widths": set(),
                "read": False, "write": False, "conditional": False,
                "ops": [],
            }
            ordered_names.append(name)
        return state[name]

    def first_call_argument(tokens: list[str]) -> list[str]:
        try:
            start = tokens.index("(") + 1
        except ValueError:
            return []
        depth = 0
        out: list[str] = []
        for token in tokens[start:]:
            if token == "(" :
                depth += 1
            elif token == ")":
                if depth == 0:
                    break
                depth -= 1
            elif token == "," and depth == 0:
                break
            out.append(token)
        return out

    for cur in body.walk_preorder():
        if cur.kind != cindex.CursorKind.CALL_EXPR:
            continue
        tokens = _token_spellings(tu.get_tokens(extent=cur.extent))
        ref = cur.referenced
        callee = ref.spelling if ref is not None else (tokens[0] if tokens else "")
        access = _MEMORY_ACCESS.search(callee or "")
        if not access:
            continue
        first_arg = first_call_argument(tokens)
        used = [name for name in macro_names if name in first_arg]
        for name in used:
            item = add_name(name)
            item["widths"].add(int(access.group(1)) // 8)
            if access.group(0).lower().startswith("read"):
                item["read"] = True
            else:
                item["write"] = True
            item["conditional"] |= in_branch(cur.location.line)
            callee_lower = (callee or "").lower()
            if "orwrite" in callee_lower:
                operation = "orwrite"
            elif "andwrite" in callee_lower:
                operation = "andwrite"
            elif "xorwrite" in callee_lower:
                operation = "xorwrite"
            elif "write" in callee_lower:
                operation = "write"
            else:
                operation = "read"
            item["ops"].append((
                operation, _call_argument_value(tu, cur, 1, definitions),
            ))

    # 非 helper 形式的直接寄存器地址访问没有可靠的读写方向，但带 REG
    # 语义的源码宏仍应登录为输入变量；数据常量（*_DAT_*）不会混入。
    for name in macro_names:
        if name not in state and "_REG_" in name:
            item = add_name(name)
            item["read"] = True

    result: list[MemoryVar] = []
    for name in ordered_names:
        item = state[name]
        prefix = _MEMORY_PREFIX.match(name)
        fallback = int(prefix.group(1)) if prefix else 4
        widths = item["widths"]
        width = max(widths) if widths else fallback
        input_value, expected_value = _derive_memory_values(item["ops"], width)
        result.append(MemoryVar(
            name=_memory_name_for_width(name, width),
            address=item["address"],
            width=width,
            read=item["read"],
            write=item["write"],
            conditional=item["conditional"],
            input_value=input_value,
            expected_value=expected_value,
        ))

    # 如果被测函数已经有返回值或可写指针，调用者可以通过这些高层结果
    # 表达分支差异；仅写寄存器不会再增加有效测试分歧。无返回/无指针的
    # 初始化函数则保留写寄存器，寄存器本身就是它的可观测结果。读寄存器
    # 始终保留，因为它可能是分支条件的输入（例如 Dma_Error）。
    has_high_level_output = (
        target.result_type.spelling not in ("", "void")
        or any("*" in child.type.spelling.replace(" ", "")
               for child in _children(target)
               if child.kind == cindex.CursorKind.PARM_DECL)
    )
    if has_high_level_output:
        result = [item for item in result if item.read]

    # 最小可观察集合：优先选择分支内访问，再按访问宽度各保留首个代表。
    # 例如 p_vog_dma_init 会稳定得到一个 32 位 PDMA 寄存器和一个
    # 16 位 ICDMA 寄存器；DMA04/05 的重复写入不会膨胀测试输入列。
    pool = [item for item in result if item.conditional] or result
    selected: list[MemoryVar] = []
    selected_widths: set[int] = set()
    for item in pool:
        if item.width in selected_widths:
            continue
        selected.append(item)
        selected_widths.add(item.width)
    return selected


# ---------------------------------------------------------------- 配置常量传播

def _normalise_expr(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _lvalue_path(cur):
    """返回源码左值的 ``(根变量, [(index|field, value), ...])``。"""
    cur = _strip_value_wrappers(cur)
    if cur.kind == cindex.CursorKind.DECL_REF_EXPR:
        ref = cur.referenced
        if ref is not None and ref.kind == cindex.CursorKind.VAR_DECL:
            return ref.spelling, []
        return None
    if cur.kind == cindex.CursorKind.ARRAY_SUBSCRIPT_EXPR:
        children = _children(cur)
        if len(children) < 2:
            return None
        base = _lvalue_path(children[0])
        index, _ = _value_of(children[1])
        if base is None or index is None:
            return None
        name, path = base
        return name, [*path, ("index", index)]
    if cur.kind == cindex.CursorKind.MEMBER_REF_EXPR:
        children = _children(cur)
        ref = cur.referenced
        if not children or ref is None or ref.kind != cindex.CursorKind.FIELD_DECL:
            return None
        base = _lvalue_path(children[0])
        if base is None:
            return None
        name, path = base
        field_index = _field_index(ref)
        if field_index is None:
            return None
        return name, [*path, ("field", (ref.spelling, field_index))]
    return None


def _definition_decl(name: str, translation_units) -> object | None:
    """在源码上下文 TU 中找带初始化器的全局定义，不接受 extern 声明。"""
    for tu in translation_units:
        for cur in tu.cursor.walk_preorder():
            if cur.kind != cindex.CursorKind.VAR_DECL or cur.spelling != name:
                continue
            try:
                is_definition = cur.is_definition()
            except Exception:
                is_definition = False
            has_initializer = any(
                child.kind in (
                    cindex.CursorKind.INIT_LIST_EXPR,
                    cindex.CursorKind.INTEGER_LITERAL,
                    cindex.CursorKind.UNEXPOSED_EXPR,
                    cindex.CursorKind.CSTYLE_CAST_EXPR,
                )
                for child in cur.get_children()
            )
            if is_definition and has_initializer:
                return cur
    return None


def _initializer_root(decl):
    children = _children(decl)
    for child in children:
        if child.kind == cindex.CursorKind.INIT_LIST_EXPR:
            return child
    for child in children:
        if child.kind in (
            cindex.CursorKind.INTEGER_LITERAL,
            cindex.CursorKind.UNEXPOSED_EXPR,
            cindex.CursorKind.CSTYLE_CAST_EXPR,
        ):
            return child
    return None


def _field_index(field) -> int | None:
    parent = field.semantic_parent
    if parent is None:
        return None
    fields = [child for child in parent.get_children()
              if child.kind == cindex.CursorKind.FIELD_DECL]
    for index, child in enumerate(fields):
        if child.spelling == field.spelling:
            return index
    return None


def _initializer_at(decl, path):
    """按数组下标和结构体字段路径取初始化器节点。"""
    current = _initializer_root(decl)
    if current is None:
        return None
    for kind, value in path:
        current = _strip_wrappers(current)
        if current.kind != cindex.CursorKind.INIT_LIST_EXPR:
            return None
        children = _children(current)
        if kind == "index":
            index = value
        else:
            # path 在 _lvalue_path 中已经保存了 FieldDecl 的声明顺序。
            # 聚合初始化器通常是 positional INIT_LIST_EXPR，不能从初值
            # 节点反向拿到字段名，所以这里不能依赖调试符号或字符串猜测。
            index = value[1] if isinstance(value, tuple) else None
            if index is None:
                return None
        if index < 0 or index >= len(children):
            return None
        current = children[index]
    return current


def _simple_binary_operator(tu, cur) -> str | None:
    tokens = _token_spellings(tu.get_tokens(extent=cur.extent))
    depth = 0
    operators = {"+", "-", "*", "/", "%", "|", "&", "^", "<<", ">>"}
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0 and token in operators:
            return token
    return None


def _const_value_expr(cur, tu, translation_units, seen=None) -> int | None:
    """求能由源码初始化器确定的整数表达式值。"""
    if cur is None:
        return None
    seen = set() if seen is None else seen
    cur = _strip_value_wrappers(cur)
    children = _children(cur)
    if cur.kind == cindex.CursorKind.INTEGER_LITERAL:
        value, _ = _value_of(cur)
        if value is not None:
            return value
        macro_name = _macro_name_at_cursor(cur)
        if macro_name:
            for context_tu in translation_units:
                value = _macro_constant_definitions(context_tu).get(macro_name)
                if value is not None:
                    return value
        return None
    if cur.kind == cindex.CursorKind.ENUM_CONSTANT_DECL:
        return cur.enum_value
    if cur.kind == cindex.CursorKind.DECL_REF_EXPR:
        value, _ = _value_of(cur)
        if value is not None:
            return value
        path = _lvalue_path(cur)
        path_key = (path[0], tuple(path[1])) if path is not None else None
        if path is None or path_key in seen:
            return None
        seen.add(path_key)
        decl = _definition_decl(path[0], translation_units)
        init = _initializer_at(decl, path[1]) if decl is not None else None
        return _const_value_expr(init, tu, translation_units, seen)
    if cur.kind in (cindex.CursorKind.MEMBER_REF_EXPR,
                    cindex.CursorKind.ARRAY_SUBSCRIPT_EXPR):
        path = _lvalue_path(cur)
        path_key = (path[0], tuple(path[1])) if path is not None else None
        if path is None or path_key in seen:
            return None
        seen.add(path_key)
        decl = _definition_decl(path[0], translation_units)
        init = _initializer_at(decl, path[1]) if decl is not None else None
        return _const_value_expr(init, tu, translation_units, seen)
    if cur.kind == cindex.CursorKind.UNARY_OPERATOR and children:
        value = _const_value_expr(children[-1], tu, translation_units, seen)
        if value is None:
            return None
        text = _tokens_text_obj(cur).replace(" ", "")
        if text.startswith("!"):
            return int(not value)
        if text.startswith("-"):
            return -value
        return value
    if cur.kind == cindex.CursorKind.BINARY_OPERATOR and len(children) == 2:
        op = _operator_of(tu, cur) or _simple_binary_operator(tu, cur)
        left = _const_value_expr(children[0], tu, translation_units, seen)
        right = _const_value_expr(children[1], tu, translation_units, seen)
        if left is None or right is None or op is None:
            return None
        if op == "+": return left + right
        if op == "-": return left - right
        if op == "*": return left * right
        if op == "/" and right != 0: return left // right
        if op == "%" and right != 0: return left % right
        if op == "|": return left | right
        if op == "&": return left & right
        if op == "^": return left ^ right
        if op == "<<": return left << right
        if op == ">>": return left >> right
    return None


def _condition_constant(cond, tu, translation_units) -> tuple[bool | None, str | None]:
    cur = _strip_wrappers(cond)
    children = _children(cur)
    if cur.kind == cindex.CursorKind.UNARY_OPERATOR and children:
        op_text = _tokens_text_obj(cur).replace(" ", "")
        if op_text.startswith("!"):
            value, _ = _condition_constant(children[-1], tu, translation_units)
            if value is not None:
                return (not value, f"{_tokens_text(tu, cond)} => {'FALSE' if value else 'TRUE'}")
    if cur.kind == cindex.CursorKind.BINARY_OPERATOR and len(children) == 2:
        op = _operator_of(tu, cur)
        if op in COMPARE_OPS:
            left = _const_value_expr(children[0], tu, translation_units)
            right = _const_value_expr(children[1], tu, translation_units)
            if left is not None and right is not None:
                result = {
                    "==": left == right, "!=": left != right,
                    "<": left < right, "<=": left <= right,
                    ">": left > right, ">=": left >= right,
                }[op]
                return result, (
                    f"{_tokens_text(tu, cond)} => {'TRUE' if result else 'FALSE'} "
                    f"(AST values: {left} {op} {right})"
                )
        if op in LOGIC_OPS:
            left = _condition_constant(children[0], tu, translation_units)[0]
            right = _condition_constant(children[1], tu, translation_units)[0]
            if left is not None and right is not None:
                result = (left and right) if op == "&&" else (left or right)
                return result, (
                    f"{_tokens_text(tu, cond)} => {'TRUE' if result else 'FALSE'}"
                )
    value = _const_value_expr(cur, tu, translation_units)
    if value is not None:
        return bool(value), f"{_tokens_text(tu, cond)} => {'TRUE' if value else 'FALSE'}"
    return None, None


def _constant_lvalues(cond, tu, translation_units) -> dict[str, int]:
    values: dict[str, int] = {}
    for node in cond.walk_preorder():
        path = _lvalue_path(node)
        if path is None:
            continue
        value = _const_value_expr(node, tu, translation_units)
        if value is not None:
            text = _tokens_text(tu, node)
            if text:
                values[_normalise_expr(text)] = value
    return values


def _condition_node(kind, children):
    if kind == "switch":
        return children[0] if children else None
    if kind == "dowhile":
        return children[-1] if children else None
    if kind == "for":
        return children[1] if len(children) >= 2 \
            and children[1].kind != cindex.CursorKind.COMPOUND_STMT else None
    return children[0] if children else None


def _annotate_constant_values(tu, body, ir, context_tus=()) -> None:
    """用配置定义 TU 对分支和控制变量做确定性常量传播。"""
    translation_units = (tu, *tuple(context_tus))
    branch_nodes = []
    for cur in body.walk_preorder():
        kind = STMT_KIND_MAP.get(cur.kind)
        if kind is None:
            continue
        children = _children(cur)
        if kind in ("while", "dowhile"):
            cond = _condition_node(kind, children)
            if cond is not None and cond.kind == cindex.CursorKind.INTEGER_LITERAL:
                continue
        branch_nodes.append((kind, _condition_node(kind, children)))

    constants: dict[str, int] = {}
    for branch, (kind, cond) in zip(ir.branches, branch_nodes):
        if cond is None or kind == "switch":
            continue
        value, reason = _condition_constant(cond, tu, translation_units)
        branch.constant_value = value
        branch.constant_reason = reason
        constants.update(_constant_lvalues(cond, tu, translation_units))

    for cv in ir.control_vars:
        value = constants.get(_normalise_expr(cv.var))
        if value is not None:
            cv.constant_value = value
            cv.constant_reason = "源码配置初始化器传播"


def find_definition_sources(source_root: Path, names: set[str]) -> tuple[Path, ...]:
    """按全局名查找可能包含初始化器的 C 源文件。

    这里只做确定性的文本预筛选，最终是否为定义由 libclang AST 的
    ``VarDecl.is_definition()`` 和初始化器节点确认；不会读取 WinAMS
    工程、map/out 或测试结果。
    """
    wanted = sorted(name for name in names if name)
    if not wanted:
        return ()
    patterns = [re.compile(
        rf"\b{re.escape(name)}\b\s*(?:\[[^\n;{{}}]*\])?\s*=",
    ) for name in wanted]
    found: list[Path] = []
    for path in sorted(source_root.rglob("*.c"), key=lambda item: item.as_posix().lower()):
        try:
            text = path.read_bytes().decode("cp932", errors="replace")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in patterns):
            found.append(path)
    return tuple(found)


# ---------------------------------------------------------------- 主入口

STMT_KIND_MAP = {
    cindex.CursorKind.IF_STMT: "if",
    cindex.CursorKind.WHILE_STMT: "while",
    cindex.CursorKind.DO_STMT: "dowhile",
    cindex.CursorKind.FOR_STMT: "for",
    cindex.CursorKind.SWITCH_STMT: "switch",
    cindex.CursorKind.CONDITIONAL_OPERATOR: "ternary",
}


def extract_function(tu, source: Path, function_name: str,
                     defines: Optional[dict] = None,
                     watch_macros=WATCH_MACROS_DEFAULT,
                     context_tus=()) -> FunctionIR:
    macros = _macro_lines(tu, tuple(watch_macros))

    target = None
    all_funcs = []
    src_name = Path(source).name
    for cur in tu.cursor.walk_preorder():
        if cur.kind == cindex.CursorKind.FUNCTION_DECL and cur.is_definition():
            try:
                if Path(cur.location.file.name).name != src_name:
                    continue   # 排除系统/头文件函数（libc 等）
            except Exception:
                continue
            all_funcs.append(cur.spelling)
            if cur.spelling == function_name:
                target = cur
    if target is None:
        raise RuntimeError(f"未找到函数定义 {function_name}；文件内函数: {all_funcs[:20]}")

    # 函数级错误甄别：文件其他函数的诊断不阻塞本函数（通用框架原则），
    # 落在目标函数行范围内的错误才致命。
    func_errors = []
    other_errors = 0
    for d in getattr(tu, "_ut_agent_errors", []):
        try:
            if target.location.line <= d.location.line <= target.extent.end.line:
                func_errors.append(d.spelling)
            else:
                other_errors += 1
        except Exception:
            other_errors += 1
    if func_errors:
        raise RuntimeError(f"{function_name} 自身存在解析错误: {func_errors[:4]}")

    ir = FunctionIR(
        name=target.spelling, file=str(source), line=target.location.line,
        line_end=target.extent.end.line,
        ret_type=target.result_type.spelling,
        config={k: (v if v != "" else "<empty>") for k, v in (defines or {}).items()},
    )
    ir.params = _params(target)

    body = next((k for k in _children(target)
                 if k.kind == cindex.CursorKind.COMPOUND_STMT), None)
    if body is None:
        raise RuntimeError(f"{function_name} 无函数体")

    ir.memory_vars = _memory_vars(tu, target, body)
    _collect_vars(body, ir)
    _collect_calls(body, tu, ir, macros)   # 在 vars 之后：需要 globals_used 判定指针表调用
    _collect_branches(body, tu, ir, macros)
    ir.enums = _collect_enums(tu)
    ir.global_writes = assign.global_writes(body, tu)
    ir.control_vars = _classify_control_vars(
        ir, assign.trace_assigns(body, tu))
    _annotate_constant_values(tu, body, ir, context_tus)

    if other_errors:
        ir.notes.append(
            f"文件其他函数存在 {other_errors} 个诊断错误（不影响 {function_name} 抽取，"
            f"函数级容忍）")

    # 疑难点登记：变量-变量比较、解析不出边界的原子条件 → M2 flow / LLM 兜底素材
    for b in ir.branches:
        for a in b.atoms:
            if a.boundary is None and a.boundary_name is None:
                ir.notes.append(
                    f"{b.bid}: 原子条件 [{a.text}] 无字面边界（变量对变量?），需 flow/LLM")
    return ir


def _params(target):
    out = []
    for a in _children(target):
        if a.kind == cindex.CursorKind.PARM_DECL:
            spelling = a.type.spelling
            is_ptr = "*" in spelling.replace(" ", "")
            out.append(Param(
                name=a.spelling, type=spelling,
                is_ptr=is_ptr, is_const="const" in spelling.lower(),
            ))
    return out


def _collect_calls(body, tu, ir, macros) -> None:
    seen: dict[str, CallSite] = {}
    for cur in body.walk_preorder():
        if cur.kind != cindex.CursorKind.CALL_EXPR:
            continue
        callee = None
        ref = cur.referenced
        if ref is not None and ref.kind == cindex.CursorKind.FUNCTION_DECL:
            callee = ref.spelling
        else:
            # 函数指针调用：取调用表达式首 token 作 callee 记号
            toks = _token_spellings(tu.get_tokens(extent=cur.extent))
            callee = toks[0] if toks else "<unknown>"
        # 结构体分发表（CanIfDispatchConfig.CanIfXxx(...)）会被 referenced 解析成
        # 空参数函数——callee 命中全局变量名时一律按指针表调用处理（介入点）
        via_table = callee in ir.globals_used
        is_ptr_call = (ref is None) or via_table
        table_base = table_member = None
        arg_types: list[str] = []
        if is_ptr_call:
            toks = _token_spellings(tu.get_tokens(extent=cur.extent))
            if len(toks) >= 3 and toks[0] in ir.globals_used:
                table_base = toks[0]
                if toks[1] == "." and toks[2] != "(":
                    table_member = toks[2]
            for ch in _children(cur)[1:]:   # 实参类型（首子是 callee 表达式）
                try:
                    arg_types.append(ch.type.spelling)
                except Exception:
                    arg_types.append("int")
        if is_ptr_call:
            dedup = f"{table_base}.{table_member}" if (table_base and table_member) \
                else (table_base or callee)
        else:
            dedup = callee
        if dedup in seen:
            continue
        site = CallSite(order=len(seen), callee=callee, line=cur.location.line,
                        via_macro=_macro_at_line(cur.location.line, macros),
                        ptr_call=is_ptr_call,
                        table_base=table_base, table_member=table_member,
                        arg_types=arg_types)
        if ref is not None and ref.kind == cindex.CursorKind.FUNCTION_DECL:
            site.ret_type = ref.result_type.spelling
            try:
                site.is_static = ref.storage_class == cindex.StorageClass.STATIC
            except Exception:
                site.is_static = False
            for i, a in enumerate(ref.get_children()):
                if a.kind == cindex.CursorKind.PARM_DECL:
                    t = a.type.spelling
                    site.params.append(Param(
                        name=a.spelling or f"arg{i}",   # 原型中的未命名形参
                        type=t,
                        is_ptr="*" in t.replace(" ", ""),
                        is_const="const" in t.lower(),
                    ))
        seen[dedup] = site
    ir.calls = list(seen.values())


def _collect_branches(body, tu, ir, macros) -> None:
    # 先找 else-if 链关系：链内 IfStmt id → 链序号
    chain: dict[int, int] = {}
    for cur in body.walk_preorder():
        if cur.kind != cindex.CursorKind.IF_STMT:
            continue
        ch = _children(cur)
        if len(ch) >= 3 and ch[2].kind == cindex.CursorKind.IF_STMT:
            head = chain.get(hash(cur.hash), 0)
            chain[hash(ch[2].hash)] = head + 1
    seq = 0
    for cur in body.walk_preorder():
        kind = STMT_KIND_MAP.get(cur.kind)
        if kind is None:
            continue
        ch = _children(cur)

        # 宏脚手架过滤：do{}while(0) 等"常量条件循环"是 DET_REPORT_ERROR 之类
        # 函数宏的包裹体，不是真实业务分支，不登记
        if kind in ("while", "dowhile"):
            cond_c = ch[-1] if kind == "dowhile" and ch else (ch[0] if ch else None)
            if cond_c is not None and cond_c.kind == cindex.CursorKind.INTEGER_LITERAL:
                continue

        seq += 1
        b = Branch(bid=f"B{seq:02d}", kind=kind, line=cur.location.line)
        b.file = cur.extent.start.file.name if cur.extent.start.file else None
        b.from_macro = _macro_at_line(cur.location.line, macros)
        b.chain_index = chain.get(hash(cur.hash), 0)
        if kind == "switch":
            b.cond_text = _tokens_text(tu, ch[0]) if ch else ""
            cases: list = []
            for sub in cur.walk_preorder():
                if sub.kind == cindex.CursorKind.CASE_STMT:
                    vals = _children(sub)
                    v, name = (None, None)
                    for e in vals:
                        v, name = _value_of(e)
                        if v is not None or name is not None:
                            break
                    cases.append(Case(label=name or str(v), value=v, is_default=False))
                elif sub.kind == cindex.CursorKind.DEFAULT_STMT:
                    cases.append(Case(label="default", value=None, is_default=True))
            b.cases = cases
        elif kind == "ternary":
            b.cond_text = _tokens_text(tu, ch[0]) if ch else ""
            if ch:
                _collect_atoms(tu, ch[0], b.atoms)
        else:
            # if/while/for：条件为首子表达式（for: init,cond,inc,body；dowhile: body,cond）
            if kind == "dowhile":
                cond = ch[-1] if ch else None
            else:
                cond = ch[0] if ch else None
            if kind == "for" and ch and len(ch) >= 2:
                cond = ch[1] if ch[1].kind != cindex.CursorKind.COMPOUND_STMT else None
            b.cond_text = _tokens_text(tu, cond) if cond is not None else ""
            if cond is not None:
                _collect_atoms(tu, cond, b.atoms)
                conn = _operator_of(tu, cond)
                if conn in LOGIC_OPS:
                    b.connective = conn
        # 宏展开的分支：token 会落到宏定义体（_exp 等），条件文本以实参原子条件呈现
        if b.from_macro and b.atoms:
            b.cond_text = " && ".join(a.text for a in b.atoms) or b.cond_text
        ir.branches.append(b)


def _collect_vars(body, ir) -> None:
    globs, locs = set(), set()
    for cur in body.walk_preorder():
        if cur.kind == cindex.CursorKind.DECL_REF_EXPR and cur.referenced is not None:
            r = cur.referenced
            if r.kind == cindex.CursorKind.VAR_DECL:
                if r.semantic_parent is not None and \
                        r.semantic_parent.kind == cindex.CursorKind.TRANSLATION_UNIT:
                    globs.add(r.spelling)
                    try:
                        if "*" in r.type.spelling.replace(" ", ""):
                            ir.config_ptrs.append(r.spelling)   # 配置表指针（介入点）
                    except Exception:
                        pass
                else:
                    locs.add(r.spelling)
        elif cur.kind == cindex.CursorKind.VAR_DECL:
            locs.add(cur.spelling)
    ir.globals_used = sorted(globs)
    ir.locals = sorted(locs)
    ir.config_ptrs = sorted(set(ir.config_ptrs))


def _collect_enums(tu) -> dict:
    """TU 内（含头文件）全部具名枚举：{枚举名: {成员名: 值}}。"""
    out: dict = {}
    for cur in tu.cursor.walk_preorder():
        if cur.kind == cindex.CursorKind.ENUM_DECL and cur.spelling:
            members = {}
            for ch in cur.get_children():
                if ch.kind == cindex.CursorKind.ENUM_CONSTANT_DECL:
                    members[ch.spelling] = ch.enum_value
            if members:
                out[cur.spelling] = members
    return out


def _classify_control_vars(ir, assigns) -> list:
    """控制变量来源分类（用例表列生成依据）。
    param → 引数列设值；global → 全局列；local_from_global → 设定其来源全局；
    local/unknown → 记 needs_flow 备注。"""
    out, seen = [], set()
    param_types = {p.name: p.type for p in ir.params}

    def add(var_text, var_type=None):
        key = (var_text or "").replace(" ", "")
        if not key or key in seen:
            return
        seen.add(key)
        if key in param_types:
            src, var_type = "param", param_types[key]
        elif "->" in key:
            src = "config"          # 配置表成员（CanIf_ConfigPtr->...）：只读，不可设定
        elif any(g in key for g in ir.globals_used):
            src = "global"
        elif key in assigns:
            src = "local_from_global"
        elif key in ir.locals:
            src = "local"
        else:
            src = "unknown"
        short = key.split(".")[-1].split("[")[0]
        cv = ControlVar(
            name=short, var=var_text, source=src,
            set_via=assigns[key]["source"] if src == "local_from_global" else None,
            var_type=var_type,
        )
        out.append(cv)
        if src == "config":
            ir.notes.append(f"控制变量 [{key}] 为配置表成员（只读），需构造配置数据方可覆盖，"
                            f"记介入点")
        elif src in ("local", "unknown"):
            ir.notes.append(f"控制变量 [{key}] 来源={src}，需 flow/LLM 判定")

    for b in ir.branches:
        for a in b.atoms:
            add(a.var, a.var_type)
        if b.kind == "switch" and b.cond_text:
            add(b.cond_text)
    return out


def list_functions(tu, source: Path = None) -> list[str]:
    out = []
    src_name = Path(source).name if source else None
    for cur in tu.cursor.walk_preorder():
        if cur.kind == cindex.CursorKind.FUNCTION_DECL and cur.is_definition():
            try:
                if src_name and Path(cur.location.file.name).name != src_name:
                    continue
            except Exception:
                continue
            out.append(f"{cur.spelling}:{cur.location.line}")
    return out
