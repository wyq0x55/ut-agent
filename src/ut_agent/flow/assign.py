"""M2 flow-lite：赋值追踪（来源判定的最小实现）。

只处理一种模式：局部变量 = 引用了全局变量的表达式（如
`currMode = CanIf_Global.channelData[ControllerId].PduMode`）。
识别结果用于控制变量来源分类（local_from_global → 设定列应落在全局上）。
复杂情况（多次赋值合并、经指针写、条件赋值）留给 M2 flow / LLM 兜底。
"""
from __future__ import annotations

from clang import cindex


def _tokens_text(tu, cur) -> str:
    texts = []
    for token in tu.get_tokens(extent=cur.extent):
        if token.kind == cindex.TokenKind.COMMENT:
            continue
        try:
            text = token.spelling
        except UnicodeDecodeError:
            continue
        if text:
            texts.append(text)
    return " ".join(texts)


def _find_tu_var(cur) -> str | None:
    """子树中第一个文件作用域变量引用（全局），返回变量名。"""
    if cur.kind == cindex.CursorKind.DECL_REF_EXPR and cur.referenced is not None:
        r = cur.referenced
        if r.kind == cindex.CursorKind.VAR_DECL and r.semantic_parent is not None \
                and r.semantic_parent.kind == cindex.CursorKind.TRANSLATION_UNIT:
            return r.spelling
    for k in cur.get_children():
        found = _find_tu_var(k)
        if found:
            return found
    return None


def trace_assigns(body, tu) -> dict[str, dict]:
    """返回 {局部变量名: {"source": 赋值右值文本, "global": 引用的全局名}}。"""
    out: dict[str, dict] = {}
    for cur in body.walk_preorder():
        if cur.kind != cindex.CursorKind.BINARY_OPERATOR:
            continue
        ch = list(cur.get_children())
        if len(ch) != 2:
            continue
        toks = []
        for token in tu.get_tokens(extent=cur.extent):
            if token.kind == cindex.TokenKind.COMMENT:
                continue
            try:
                text = token.spelling
            except UnicodeDecodeError:
                continue
            if text:
                toks.append(text)
        if "=" not in toks:      # 赋值（== 是单 token，不会误命中）
            continue
        lhs, rhs = ch
        target = None
        if lhs.kind == cindex.CursorKind.DECL_REF_EXPR and lhs.referenced is not None \
                and lhs.referenced.kind == cindex.CursorKind.VAR_DECL \
                and lhs.referenced.semantic_parent is not None \
                and lhs.referenced.semantic_parent.kind != cindex.CursorKind.TRANSLATION_UNIT:
            target = lhs.referenced.spelling
        if target is None:
            continue
        g = _find_tu_var(rhs)
        if g is not None:
            out[target] = {"source": _tokens_text(tu, rhs), "global": g}
    return out


def global_writes(body, tu) -> list[str]:
    """被测函数内对全局变量的赋值（左值文本，按出现顺序去重）——期待列素材。"""
    seen: list[str] = []
    for cur in body.walk_preorder():
        if cur.kind != cindex.CursorKind.BINARY_OPERATOR:
            continue
        ch = list(cur.get_children())
        if len(ch) != 2:
            continue
        toks = []
        for token in tu.get_tokens(extent=cur.extent):
            if token.kind == cindex.TokenKind.COMMENT:
                continue
            try:
                text = token.spelling
            except UnicodeDecodeError:
                continue
            if text:
                toks.append(text)
        if "=" not in toks:
            continue
        lhs, _ = ch
        if _find_tu_var(lhs) is not None:
            text = _tokens_text(tu, lhs)
            if text not in seen:
                seen.append(text)
    return seen
