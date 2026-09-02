"""对 Soft 构建产物做确定性的只读证据分析。

这个模块不参与源码 AST、FunctionIR 或 TestCsv 的生成。它只读取用户明确提供的
``Soft.map``、``Soft.mot``、``Soft.out`` 和 ``Soft.out.xlo``，提取地址、代码段、
符号、DWARF 段、S-Record 地址范围以及 WinAMS OMF 头信息，供规则引擎交叉确认。
不访问网络、不调用 LLM、不使用随机数或当前时间。
"""
from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class FileEvidence:
    """一个输入文件的可复核摘要。"""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class MapSection:
    name: str
    address: int
    size: int


@dataclass(frozen=True)
class MapSymbol:
    name: str
    section: str
    address: int
    size: int


@dataclass(frozen=True)
class MapEvidence:
    file: FileEvidence
    sections: tuple[MapSection, ...]
    symbols: tuple[MapSymbol, ...]

    def find_symbols(self, name: str) -> tuple[MapSymbol, ...]:
        variants = _symbol_name_variants(name)
        return tuple(symbol for symbol in self.symbols if symbol.name in variants)

    def to_dict(self, symbol_names: Sequence[str] = ()) -> dict:
        selected = _selected_map_symbols(self, symbol_names)
        return {
            "file": asdict(self.file),
            "section_count": len(self.sections),
            "symbol_count": len(self.symbols),
            "sections": [asdict(item) for item in self.sections],
            "symbols": [asdict(item) for item in selected],
        }


@dataclass(frozen=True)
class MotRange:
    start: int
    end: int


@dataclass(frozen=True)
class MotEvidence:
    file: FileEvidence
    record_count: int
    data_record_count: int
    data_bytes: int
    address_min: int | None
    address_max: int | None
    address_ranges: tuple[MotRange, ...]
    start_address: int | None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["file"] = asdict(self.file)
        return result


@dataclass(frozen=True)
class ElfSection:
    name: str
    section_type: int
    address: int
    offset: int
    size: int
    flags: int


@dataclass(frozen=True)
class ElfSymbol:
    name: str
    address: int
    size: int
    section_index: int


@dataclass(frozen=True)
class ElfEvidence:
    file: FileEvidence
    class_bits: int
    endianness: str
    machine: int
    machine_name: str
    entry: int
    sections: tuple[ElfSection, ...]
    symbols: tuple[ElfSymbol, ...]
    dwarf_sections: tuple[str, ...]

    def find_symbols(self, name: str) -> tuple[ElfSymbol, ...]:
        variants = _symbol_name_variants(name)
        return tuple(symbol for symbol in self.symbols if symbol.name in variants)

    def to_dict(self, symbol_names: Sequence[str] = ()) -> dict:
        selected = _selected_elf_symbols(self, symbol_names)
        return {
            "file": asdict(self.file),
            "class_bits": self.class_bits,
            "endianness": self.endianness,
            "machine": self.machine,
            "machine_name": self.machine_name,
            "entry": self.entry,
            "section_count": len(self.sections),
            "symbol_count": len(self.symbols),
            "sections": [asdict(item) for item in self.sections],
            "dwarf_sections": list(self.dwarf_sections),
            "symbols": [asdict(item) for item in selected],
        }


@dataclass(frozen=True)
class XloEvidence:
    file: FileEvidence
    header_hex: str
    format_name: str
    architecture: str | None
    marker: str | None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["file"] = asdict(self.file)
        return result


@dataclass(frozen=True)
class SymbolCrossCheck:
    query: str
    map_symbols: tuple[MapSymbol, ...]
    elf_symbols: tuple[ElfSymbol, ...]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "map_symbols": [asdict(item) for item in self.map_symbols],
            "elf_symbols": [asdict(item) for item in self.elf_symbols],
        }


@dataclass(frozen=True)
class ArtifactEvidence:
    """一组构建产物的只读分析结果。"""

    root: str
    map: MapEvidence | None = None
    mot: MotEvidence | None = None
    out: ElfEvidence | None = None
    xlo: XloEvidence | None = None

    def cross_check_symbols(self, names: Iterable[str]) -> tuple[SymbolCrossCheck, ...]:
        if self.map is None and self.out is None:
            return tuple(SymbolCrossCheck(name, (), ()) for name in names)
        return tuple(
            SymbolCrossCheck(
                query=name,
                map_symbols=self.map.find_symbols(name) if self.map else (),
                elf_symbols=self.out.find_symbols(name) if self.out else (),
            )
            for name in names
        )

    def to_dict(self, symbol_names: Sequence[str] = ()) -> dict:
        return {
            "root": self.root,
            "map": self.map.to_dict(symbol_names) if self.map else None,
            "mot": self.mot.to_dict() if self.mot else None,
            "out": self.out.to_dict(symbol_names) if self.out else None,
            "xlo": self.xlo.to_dict() if self.xlo else None,
            "symbol_cross_checks": [
                item.to_dict() for item in self.cross_check_symbols(symbol_names)
            ],
        }


_MAP_SECTION = re.compile(
    r"^\s+(?P<name>\.\S+)\s+"
    r"(?P<address>[0-9A-Fa-f]+)\s+"
    r"(?P<size>[0-9A-Fa-f]+)\s+"
    r"[0-9]+\s+[0-9A-Fa-f]+\s*$"
)
_MAP_SYMBOL = re.compile(
    r"^\s+(?P<section>\.\S+)\s+"
    r"(?P<address>[0-9A-Fa-f]+)\+(?P<size>[0-9A-Fa-f]+)\s+"
    r"(?P<name>\S+)\s*$"
)


def _file_evidence(path: Path, data: bytes) -> FileEvidence:
    return FileEvidence(
        path=str(path.resolve()), size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _decode_text(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp932")
        except UnicodeDecodeError as exc:
            raise ValueError(f"无法解码产物文本：{path}") from exc


def _symbol_name_variants(name: str) -> frozenset[str]:
    variants = {name}
    if name.startswith("_"):
        variants.add(name[1:])
    else:
        variants.add("_" + name)
    return frozenset(variants)


def _selected_map_symbols(evidence: MapEvidence, names: Sequence[str]) -> tuple[MapSymbol, ...]:
    if not names:
        return ()
    wanted = frozenset().union(*(_symbol_name_variants(name) for name in names))
    return tuple(symbol for symbol in evidence.symbols if symbol.name in wanted)


def _selected_elf_symbols(evidence: ElfEvidence, names: Sequence[str]) -> tuple[ElfSymbol, ...]:
    if not names:
        return ()
    wanted = frozenset().union(*(_symbol_name_variants(name) for name in names))
    return tuple(symbol for symbol in evidence.symbols if symbol.name in wanted)


def read_map(path: Path) -> MapEvidence:
    """读取 GHS map 的段摘要和 ``section address+size symbol`` 记录。"""
    path = Path(path)
    data = path.read_bytes()
    text = _decode_text(data, path)
    sections: list[MapSection] = []
    symbols: list[MapSymbol] = []
    for line in text.splitlines():
        section_match = _MAP_SECTION.match(line)
        if section_match:
            sections.append(MapSection(
                name=section_match.group("name"),
                address=int(section_match.group("address"), 16),
                size=int(section_match.group("size"), 16),
            ))
            continue
        symbol_match = _MAP_SYMBOL.match(line)
        if symbol_match:
            symbols.append(MapSymbol(
                name=symbol_match.group("name"),
                section=symbol_match.group("section"),
                address=int(symbol_match.group("address"), 16),
                size=int(symbol_match.group("size"), 16),
            ))
    return MapEvidence(_file_evidence(path, data), tuple(sections), tuple(symbols))


_MOT_ADDRESS_BYTES = {0: 2, 1: 2, 2: 3, 3: 4, 5: 2, 6: 3, 7: 4, 8: 3, 9: 2}
_MOT_DATA_TYPES = frozenset({1, 2, 3})
_MOT_START_TYPES = frozenset({7, 8, 9})


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> tuple[MotRange, ...]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple(MotRange(start, end) for start, end in merged)


def read_mot(path: Path) -> MotEvidence:
    """读取 Motorola S-Record，校验每条记录的长度与 checksum。"""
    path = Path(path)
    data = path.read_bytes()
    record_count = 0
    data_record_count = 0
    data_bytes = 0
    address_min: int | None = None
    address_max: int | None = None
    start_address: int | None = None
    ranges: list[tuple[int, int]] = []
    for line_no, raw_line in enumerate(data.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"S-Record 第 {line_no} 行不是 ASCII：{path}") from exc
        if len(text) < 4 or text[0] not in "Ss" or text[1] not in "0123456789":
            raise ValueError(f"S-Record 第 {line_no} 行格式错误：{path}")
        record_type = int(text[1], 16)
        address_bytes = _MOT_ADDRESS_BYTES.get(record_type)
        if address_bytes is None:
            raise ValueError(f"S-Record 第 {line_no} 行类型不支持：S{record_type}")
        try:
            count = int(text[2:4], 16)
            payload = bytes.fromhex(text[4:])
        except ValueError as exc:
            raise ValueError(f"S-Record 第 {line_no} 行十六进制错误：{path}") from exc
        if len(payload) != count:
            raise ValueError(f"S-Record 第 {line_no} 行长度错误：{path}")
        if ((count + sum(payload)) & 0xFF) != 0xFF:
            raise ValueError(f"S-Record 第 {line_no} 行 checksum 错误：{path}")
        if count < address_bytes + 1:
            raise ValueError(f"S-Record 第 {line_no} 行地址长度错误：{path}")
        address = int.from_bytes(payload[:address_bytes], "big")
        record_data = payload[address_bytes:-1]
        record_count += 1
        if record_type in _MOT_DATA_TYPES:
            end = address + len(record_data)
            ranges.append((address, end))
            data_record_count += 1
            data_bytes += len(record_data)
            address_min = address if address_min is None else min(address_min, address)
            address_max = end if address_max is None else max(address_max, end)
        elif record_type in _MOT_START_TYPES:
            start_address = address
    return MotEvidence(
        file=_file_evidence(path, data), record_count=record_count,
        data_record_count=data_record_count, data_bytes=data_bytes,
        address_min=address_min, address_max=address_max,
        address_ranges=_merge_ranges(ranges), start_address=start_address,
    )


_ELF_HEADER = "HHIIIIIHHHHHH"
_ELF_SECTION_32 = "IIIIIIIIII"
_ELF_SECTION_64 = "IIQQQQIIQQ"
_ELF_SYMBOL = "IIIBBH"
_SHT_SYMTAB = 2
_SHT_STRTAB = 3
_SHT_DYNSYM = 11
_ELF_MACHINES = {0x24: "RH850"}


def _bounded_slice(data: bytes, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise ValueError(f"ELF {label} 越界")
    return data[offset:offset + size]


def _elf_string(table: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(table):
        return ""
    end = table.find(b"\0", offset)
    if end < 0:
        end = len(table)
    return table[offset:end].decode("utf-8", errors="replace")


def read_elf(path: Path) -> ElfEvidence:
    """读取 32/64 位 ELF 的 section、符号和 DWARF 段摘要。"""
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 16 or data[:4] != b"\x7fELF":
        raise ValueError(f"不是 ELF 文件：{path}")
    elf_class = data[4]
    data_encoding = data[5]
    if elf_class not in (1, 2):
        raise ValueError(f"不支持的 ELF class：{path}")
    if data_encoding not in (1, 2):
        raise ValueError(f"不支持的 ELF endian：{path}")
    prefix = "<" if data_encoding == 1 else ">"
    class_bits = 32 if elf_class == 1 else 64
    section_format = prefix + (
        _ELF_SECTION_32 if elf_class == 1 else _ELF_SECTION_64
    )
    header_format = prefix + _ELF_HEADER
    header_size = struct.calcsize(header_format)
    if len(data) < 16 + header_size:
        raise ValueError(f"ELF header 不完整：{path}")
    header = struct.unpack_from(header_format, data, 16)
    (
        _e_type, machine, _version, entry, _program_offset, section_offset,
        _flags, _ehsize, _program_entry_size, _program_count, section_entry_size,
        section_count, section_name_index,
    ) = header
    expected_section_size = struct.calcsize(section_format)
    if section_count and section_entry_size < expected_section_size:
        raise ValueError(f"ELF section header 大小错误：{path}")
    if section_count:
        _bounded_slice(
            data, section_offset, section_entry_size * section_count,
            "section table",
        )

    raw_sections: list[tuple[int, ...]] = []
    for index in range(section_count):
        raw_sections.append(struct.unpack_from(
            section_format,
            data,
            section_offset + index * section_entry_size,
        ))
    section_name_table = b""
    if section_name_index < len(raw_sections):
        section_name_header = raw_sections[section_name_index]
        section_name_table = _bounded_slice(
            data, section_name_header[4], section_name_header[5], "section names",
        )
    sections = tuple(
        ElfSection(
            name=_elf_string(section_name_table, raw[0]), section_type=raw[1],
            flags=raw[2], address=raw[3], offset=raw[4], size=raw[5],
        )
        for raw in raw_sections
    )
    symbols: list[ElfSymbol] = []
    symbol_format = prefix + _ELF_SYMBOL if elf_class == 1 else prefix + "IBBHQQ"
    symbol_size = struct.calcsize(symbol_format)
    for raw, section in zip(raw_sections, sections):
        if section.section_type not in (_SHT_SYMTAB, _SHT_DYNSYM):
            continue
        string_table = b""
        if raw[6] < len(raw_sections):
            linked = raw_sections[raw[6]]
            string_table = _bounded_slice(data, linked[4], linked[5], "symbol names")
        entry_size = raw[9] or symbol_size
        if entry_size < symbol_size or section.size % entry_size:
            raise ValueError(f"ELF symbol table 大小错误：{path}")
        table = _bounded_slice(data, section.offset, section.size, "symbol table")
        for offset in range(0, len(table), entry_size):
            item = struct.unpack_from(symbol_format, table, offset)
            if elf_class == 1:
                name_offset, address, size, _info, _other, section_index_value = item
            else:
                name_offset, _info, _other, section_index_value, address, size = item
            name = _elf_string(string_table, name_offset)
            if name:
                symbols.append(ElfSymbol(
                    name=name, address=address, size=size,
                    section_index=section_index_value,
                ))
    dwarf_sections = tuple(
        section.name for section in sections
        if section.name.startswith((".debug_", ".zdebug_"))
    )
    return ElfEvidence(
        file=_file_evidence(path, data), class_bits=class_bits,
        endianness="little" if data_encoding == 1 else "big", machine=machine,
        machine_name=_ELF_MACHINES.get(machine, f"EM_{machine}"), entry=entry,
        sections=sections, symbols=tuple(symbols), dwarf_sections=dwarf_sections,
    )


def read_xlo(path: Path) -> XloEvidence:
    """读取 GHS OMF 的稳定头部标识，不尝试改写或反汇编 XLO。"""
    path = Path(path)
    data = path.read_bytes()
    if not data:
        raise ValueError(f"XLO 文件为空：{path}")
    head = data[:256]
    if b"GHSOMF" in head or b"OMF V" in head:
        format_name = "GHS OMF"
    else:
        format_name = "unknown"
    architecture = "V850" if b"V850" in head else None
    markers = re.findall(rb"[ -~]{4,}", head)
    marker = next((item.decode("ascii") for item in markers if b"OMF" in item), None)
    return XloEvidence(
        file=_file_evidence(path, data), header_hex=head[:32].hex(),
        format_name=format_name, architecture=architecture, marker=marker,
    )


def _resolve_artifact(root: Path, override: str | Path | None, default_name: str) -> Path | None:
    if override is not None:
        candidate = Path(override)
        return candidate if candidate.is_absolute() else (root / candidate)
    candidate = root / default_name
    return candidate if candidate.is_file() else None


def analyze_artifacts(
    root: Path,
    *,
    map_path: str | Path | None = None,
    mot_path: str | Path | None = None,
    out_path: str | Path | None = None,
    xlo_path: str | Path | None = None,
) -> ArtifactEvidence:
    """按显式路径或标准文件名读取构建产物；缺失的默认产物保持为 ``None``。"""
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"产物根目录不存在：{root}")
    selected = {
        "map": _resolve_artifact(root, map_path, "Soft.map"),
        "mot": _resolve_artifact(root, mot_path, "Soft.mot"),
        "out": _resolve_artifact(root, out_path, "Soft.out"),
        "xlo": _resolve_artifact(root, xlo_path, "Soft.out.xlo"),
    }
    for kind, path in selected.items():
        if path is not None and not path.is_file():
            raise FileNotFoundError(f"指定的 {kind} 产物不存在：{path}")
    return ArtifactEvidence(
        root=str(root),
        map=read_map(selected["map"]) if selected["map"] else None,
        mot=read_mot(selected["mot"]) if selected["mot"] else None,
        out=read_elf(selected["out"]) if selected["out"] else None,
        xlo=read_xlo(selected["xlo"]) if selected["xlo"] else None,
    )
