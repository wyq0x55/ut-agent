"""WinAMS CSV format checks applied after semantic suite validation."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class CsvValidation:
    valid: bool
    errors: tuple[str, ...] = ()


def validate_csv_text(text: str) -> CsvValidation:
    errors: list[str] = []
    if "\r\n" not in text:
        errors.append("CSV 必须使用 CRLF 换行")
    if text and not text.endswith("\r\n"):
        errors.append("CSV 必须以 CRLF 结束")
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        return CsvValidation(False, (f"CSV 解析失败: {exc}",))
    if not rows or not rows[0] or rows[0][0] != "mod":
        errors.append("CSV 第一行必须以 mod 开始")
        return CsvValidation(False, tuple(errors))
    comment_index = next((index for index, row in enumerate(rows)
                          if row and row[0] == "#COMMENT"), None)
    if comment_index is None:
        errors.append("CSV 缺少 #COMMENT 行")
        return CsvValidation(False, tuple(errors))
    try:
        input_count = int(rows[0][3])
        output_count = int(rows[0][4])
    except (IndexError, ValueError):
        errors.append("mod 行缺少有效输入/输出列数")
        return CsvValidation(False, tuple(errors))
    comments = rows[comment_index][1:]
    declared = input_count + output_count
    if len(comments) != declared:
        errors.append(f"#COMMENT 列数 {len(comments)} != 声明列数 {declared}")
    width = len(rows[0])
    for index, row in enumerate(rows[comment_index + 1:], comment_index + 2):
        if not row or row[0].startswith(";$L$") or row[0] == "%":
            continue
        if row[0] == "":
            if len(row) != declared + 1:
                errors.append(f"数据行 {index} 列数 {len(row)} != {declared + 1}")
    return CsvValidation(not errors, tuple(errors))
