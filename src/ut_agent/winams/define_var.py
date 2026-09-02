"""确定性生成 WinAMS 的 IO 登录副文件 ``DefineVar.dat``。

WinAMS 没有公开的 ``DefineVar.dat`` 命令行注册接口。生成器以源码 AST
提取 memory-mapped IO 宏的地址和访问宽度；规则引擎的证据阶段可只读引用
``Soft.map``/``Soft.out`` 对地址和符号进行交叉确认。普通全局、指针和 stub
变量不生成空定义记录。原工程 DefineVar 只作为显式兼容模式的对照输入，
不是默认数据源。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from ut_agent.ir import FunctionIR


@dataclass(frozen=True)
class DefineVarEntry:
    """一条 WinAMS DefineVar 记录。"""

    name: str
    definition: str = ""
    record_type: str = "8"


def entries_from_ir(ir: FunctionIR) -> tuple[DefineVarEntry, ...]:
    """从 FunctionIR 生成 WinAMS IO 登录记录。

    ``MemoryVar.address`` 和 ``MemoryVar.width`` 由 C 宏定义及其 helper
    调用提取。WinAMS 的定义字段格式为 ``地址#U类型#字节数``，例如
    ``0xFFFFB080#U2#2``。
    """
    entries: list[DefineVarEntry] = []
    for memory in ir.memory_vars:
        width = memory.width if memory.width in (1, 2, 4, 8) else 4
        entry = DefineVarEntry(
            name=memory.name,
            definition=f"0x{memory.address:X}#U{width}#{width}",
        )
        entries.append(entry)
    return tuple(entries)


def read_define_var(path: Path) -> tuple[DefineVarEntry, ...]:
    """读取 CP932/CRLF 的 WinAMS ``DefineVar.dat``，过滤空定义记录。"""
    text = path.read_bytes().decode("cp932")
    rows = csv.reader(io.StringIO(text, newline=""))
    entries: list[DefineVarEntry] = []
    for line_no, row in enumerate(rows, start=1):
        if not row or all(not item for item in row):
            continue
        if len(row) < 3 or not row[1]:
            raise ValueError(f"DefineVar.dat 第 {line_no} 行格式错误：{path}")
        if not row[2]:
            continue
        entries.append(DefineVarEntry(
            name=row[1], definition=row[2], record_type=row[0] or "8",
        ))
    return tuple(entries)


def render_define_var(entries: tuple[DefineVarEntry, ...] | list[DefineVarEntry]) -> str:
    """按 WinAMS 格式渲染 DefineVar 内容；空定义记录不输出。"""
    output = io.StringIO(newline="")

    def quote(value: str) -> str:
        field = io.StringIO(newline="")
        csv.writer(field, lineterminator="", quoting=csv.QUOTE_ALL).writerow([value])
        return field.getvalue()

    for entry in entries:
        if not entry.definition:
            continue
        output.write(
            f"{entry.record_type},{quote(entry.name)},{quote(entry.definition)}\r\n"
        )
    return output.getvalue()


def render_winams_ini(define_var: Path, *, windows_path: str | None = None) -> str:
    """生成最小 WinAMS.INI，显式绑定本工程的 DefineVar.dat。"""
    define_path = windows_path or str(define_var)
    return (
        "[WinAMS]\r\n"
        f"DefVarFile={define_path}\r\n"
        ""
    )
