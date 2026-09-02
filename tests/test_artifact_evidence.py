import json
import struct

from ut_agent import cli
from ut_agent.artifacts import (
    analyze_artifacts,
    read_elf,
    read_map,
    read_mot,
    read_xlo,
)


def _srecord(record_type: int, address: int, data: bytes = b"") -> str:
    address_size = {0: 2, 1: 2, 2: 3, 3: 4, 5: 2, 6: 3, 7: 4, 8: 3, 9: 2}[record_type]
    address_data = address.to_bytes(address_size, "big") + data
    count = len(address_data) + 1
    checksum = (~(count + sum(address_data))) & 0xFF
    return f"S{record_type:X}{count:02X}{address_data.hex()}{checksum:02X}".upper()


def _minimal_elf() -> bytes:
    section_names = b"\0.shstrtab\0.text\0.symtab\0.strtab\0.debug_info\0"
    string_names = b"\0target\0"
    text = b"\x00\x00\x00\x00"
    symbols = b"\0" * 16 + struct.pack(
        "<IIIBBH", string_names.index(b"target"), 0x1000, 4, 0x12, 0, 2
    )
    debug_info = b"\x01\x02"

    blobs = {1: section_names, 2: text, 3: symbols, 4: string_names, 5: debug_info}
    offsets = {}
    cursor = 52
    result = bytearray(b"\0" * cursor)
    for index in range(1, 6):
        offsets[index] = cursor
        result.extend(blobs[index])
        cursor += len(blobs[index])
    section_offset = cursor
    section_header = "<IIIIIIIIII"
    sections = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (section_names.index(b".shstrtab"), 3, 0, 0, offsets[1], len(section_names), 0, 0, 1, 0),
        (section_names.index(b".text"), 1, 6, 0x1000, offsets[2], len(text), 0, 0, 4, 0),
        (section_names.index(b".symtab"), 2, 0, 0, offsets[3], len(symbols), 4, 1, 4, 16),
        (section_names.index(b".strtab"), 3, 0, 0, offsets[4], len(string_names), 0, 0, 1, 0),
        (section_names.index(b".debug_info"), 1, 0, 0, offsets[5], len(debug_info), 0, 0, 1, 0),
    ]
    for section in sections:
        result.extend(struct.pack(section_header, *section))

    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\0" * 8
    header = struct.pack(
        "<HHIIIIIHHHHHH",
        1, 0x24, 1, 0x1000, 0, section_offset, 0,
        52, 0, 0, 40, len(sections), 1,
    )
    result[:16] = ident
    result[16:52] = header
    return bytes(result)


def test_map_extracts_sections_and_symbols(tmp_path):
    path = tmp_path / "Soft.map"
    path.write_text(
        "Image Summary\n"
        "  .text 00100000 00000020 32 0000000\n"
        " .text            00100000+000020   _target\n"
        " .text            00100020+000004   _other\n",
        encoding="ascii",
    )

    evidence = read_map(path)

    assert len(evidence.sections) == 1
    assert evidence.sections[0].name == ".text"
    assert evidence.sections[0].address == 0x100000
    assert evidence.sections[0].size == 0x20
    assert evidence.find_symbols("target")[0].address == 0x100000
    assert evidence.find_symbols("target")[0].size == 0x20


def test_mot_validates_checksum_and_merges_data_ranges(tmp_path):
    path = tmp_path / "Soft.mot"
    path.write_text(
        "\n".join([
            _srecord(0, 0, b"Soft"),
            _srecord(1, 0x1000, b"\x01\x02"),
            _srecord(1, 0x1002, b"\x03\x04"),
            _srecord(9, 0x1000),
        ]) + "\n",
        encoding="ascii",
    )

    evidence = read_mot(path)

    assert evidence.record_count == 4
    assert evidence.data_record_count == 2
    assert evidence.data_bytes == 4
    assert evidence.address_min == 0x1000
    assert evidence.address_max == 0x1004
    assert evidence.address_ranges[0].start == 0x1000
    assert evidence.address_ranges[0].end == 0x1004
    assert evidence.start_address == 0x1000


def test_elf_extracts_rh850_symbols_and_dwarf_sections(tmp_path):
    path = tmp_path / "Soft.out"
    path.write_bytes(_minimal_elf())

    evidence = read_elf(path)

    assert evidence.class_bits == 32
    assert evidence.endianness == "little"
    assert evidence.machine_name == "RH850"
    assert ".debug_info" in evidence.dwarf_sections
    assert evidence.find_symbols("target")[0].address == 0x1000


def test_artifact_bundle_cross_checks_map_elf_and_xlo(tmp_path):
    (tmp_path / "Soft.map").write_text(
        " .text 00100000 00000020 32 0000000\n"
        " .text 00100000+000020 _target\n",
        encoding="ascii",
    )
    (tmp_path / "Soft.mot").write_text(
        _srecord(1, 0x1000, b"\x01") + "\n" + _srecord(9, 0x1000) + "\n",
        encoding="ascii",
    )
    (tmp_path / "Soft.out").write_bytes(_minimal_elf())
    (tmp_path / "Soft.out.xlo").write_bytes(b"\x04Q\0\0V850GHSOMF V01.28")

    evidence = analyze_artifacts(tmp_path)
    result = evidence.to_dict(["target"])

    assert all(evidence_item is not None for evidence_item in (
        evidence.map, evidence.mot, evidence.out, evidence.xlo,
    ))
    assert result["xlo"]["format_name"] == "GHS OMF"
    assert result["xlo"]["architecture"] == "V850"
    assert result["symbol_cross_checks"][0]["map_symbols"][0]["name"] == "_target"
    assert result["symbol_cross_checks"][0]["elf_symbols"][0]["name"] == "target"


def test_artifacts_cli_emits_json(capsys, tmp_path):
    (tmp_path / "Soft.map").write_text(
        " .text 00100000 00000020 32 0000000\n"
        " .text 00100000+000020 _target\n",
        encoding="ascii",
    )

    assert cli.main(["artifacts", str(tmp_path), "--symbol", "target"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["map"]["symbol_count"] == 1
    assert result["symbol_cross_checks"][0]["query"] == "target"
