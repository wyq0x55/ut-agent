"""Arm GNU Toolchain 构建器（确定性、本地 subprocess、无网络）。

WinAMS 只需要函数隔离后的带调试信息 ELF。参考工程的原始 ``Soft.out``
是 RH850/GHS 产物，不能直接拿给 Arm 链接器；本模块因此按源文件编译，
使用函数/数据分节和 ``--gc-sections`` 保留目标函数，生成 ARM ELF。
Windows 安装的 ``arm-none-eabi-gcc.exe`` 从 WSL 调用时必须经过
PowerShell，否则 GCC 找不到同目录下的 ``cc1``。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_ARM_GCC_CANDIDATES = (
    r"C:\Program Files\Arm\GNU Toolchain mingw-w64-x86_64-arm-none-eabi\bin\arm-none-eabi-gcc.exe",
    r"C:\Program Files (x86)\Arm\GNU Toolchain mingw-w64-x86_64-arm-none-eabi\bin\arm-none-eabi-gcc.exe",
    "/mnt/c/Program Files/Arm/GNU Toolchain mingw-w64-x86_64-arm-none-eabi/bin/arm-none-eabi-gcc.exe",
    "/mnt/c/Program Files (x86)/Arm/GNU Toolchain mingw-w64-x86_64-arm-none-eabi/bin/arm-none-eabi-gcc.exe",
)
_ARM_GCC_OMF_CANDIDATES = (
    r"C:\WinAMS\BIN\armgccomf.EXE",
    r"C:\WinAMS\BIN\ARMGccOmf.EXE",
    "/mnt/c/WinAMS/BIN/armgccomf.EXE",
    "/mnt/c/WinAMS/BIN/ARMGccOmf.EXE",
)


@dataclass(frozen=True)
class ArmGccConfig:
    compiler: str | Path
    cpu: str = "cortex-m4"
    thumb: bool = True
    debug: bool = True
    function_sections: bool = True
    data_sections: bool = True


def _existing_tool(item: str) -> Path | None:
    path = Path(item)
    if path.is_file():
        return path
    if re.match(r"^[A-Za-z]:[\\/]", item) and shutil.which("wslpath"):
        result = subprocess.run(
            ["wslpath", "-u", item], check=False, capture_output=True, text=True
        )
        if result.returncode == 0:
            converted = Path(result.stdout.strip())
            if converted.is_file():
                return converted
    return None


def find_arm_gcc(explicit: str | Path | None = None) -> Path:
    """按显式路径、环境变量、WSL PATH、Arm 官方安装路径查找编译器。"""
    choices: list[str] = []
    if explicit:
        choices.append(str(explicit))
    if os.environ.get("ARM_NONE_EABI_GCC"):
        choices.append(os.environ["ARM_NONE_EABI_GCC"])
    found = shutil.which("arm-none-eabi-gcc")
    if found:
        choices.append(found)
    choices.extend(_ARM_GCC_CANDIDATES)
    for item in choices:
        path = _existing_tool(item)
        if path is not None:
            return path
    raise FileNotFoundError(
        "找不到 arm-none-eabi-gcc；请设置 ARM_NONE_EABI_GCC 或安装 Arm GNU Toolchain"
    )


def find_arm_omf_converter(explicit: str | Path | None = None) -> Path:
    """查找 WinAMS 随附的 ARM GCC OMF Converter。"""
    choices = [str(explicit)] if explicit else []
    choices.extend(_ARM_GCC_OMF_CANDIDATES)
    for item in choices:
        path = _existing_tool(item)
        if path is not None:
            return path
    raise FileNotFoundError(
        "找不到 WinAMS armgccomf.EXE；请设置 --omf-converter 或安装 WinAMS"
    )


def _is_windows_executable(path: str | Path) -> bool:
    text = str(path)
    return text.lower().endswith((".exe", ".cmd", ".bat")) or bool(
        re.match(r"^[A-Za-z]:[\\/]", text)
    )


def _wsl_to_windows(path: str | Path) -> str:
    text = str(path)
    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
        return text
    wslpath = shutil.which("wslpath")
    if not wslpath:
        return text
    result = subprocess.run(
        [wslpath, "-w", text], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_executable(executable: str | Path,
                    args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    executable = str(executable)
    if _is_windows_executable(executable):
        native_executable = _wsl_to_windows(executable)
        native_args = [
            _wsl_to_windows(a) if not a.startswith("-") else a for a in args
        ]
        if os.name == "nt":
            # Git Bash 启动的 Windows Python 可以直接调用原生 .exe；不再
            # 隐式切换到 PowerShell，保持实验编排 shell 的可追溯性。
            return subprocess.run(
                [native_executable, *native_args], check=True,
                capture_output=True, text=True, encoding="cp932", errors="replace",
            )
        ps = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        command = "& " + _powershell_quote(native_executable)
        command += " " + " ".join(_powershell_quote(value)
                                    for value in native_args)
        return subprocess.run(
            ps + [command], check=True, capture_output=True, text=True,
            encoding="cp932", errors="replace",
        )
    return subprocess.run([executable, *args], check=True, capture_output=True, text=True)


def _run(config: ArmGccConfig, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return _run_executable(config.compiler, args)


def _common_flags(config: ArmGccConfig) -> list[str]:
    flags = []
    if config.debug:
        flags.append("-g")
    if config.cpu:
        flags.append(f"-mcpu={config.cpu}")
    if config.thumb:
        flags.append("-mthumb")
    if config.function_sections:
        flags.append("-ffunction-sections")
    if config.data_sections:
        flags.append("-fdata-sections")
    return flags


def _include_flags(include_dirs: Sequence[Path | str], defines: dict[str, str]) -> list[str]:
    flags: list[str] = []
    for directory in include_dirs:
        flags += ["-I", str(directory)]
    for name, value in defines.items():
        flags.append(f"-D{name}={value}" if value != "" else f"-D{name}")
    return flags


def _winams_stub_symbols(sources: Sequence[Path]) -> tuple[str, ...]:
    """保留交给 WinAMS 接管的 AMSTB 符号，避免 --gc-sections 丢掉它们。"""
    names: set[str] = set()
    pattern = re.compile(r"\b(AMSTB_[A-Za-z_][A-Za-z0-9_]*)\s*\(")
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(pattern.findall(text))
    return tuple(sorted(names))


def build_object(source: Path, output: Path, config: ArmGccConfig,
                 include_dirs: Sequence[Path | str] = (),
                 defines: dict[str, str] | None = None) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    args = _common_flags(config) + [
        "-ffreestanding", "-fno-builtin", "-c", str(source), "-o", str(output),
    ] + _include_flags(include_dirs, defines or {})
    _run(config, args)
    return output


def build_elf(sources: Sequence[Path], output: Path, config: ArmGccConfig,
              include_dirs: Sequence[Path | str] = (),
              defines: dict[str, str] | None = None,
              entry: str | None = None,
              allow_unresolved: bool = True) -> tuple[Path, tuple[Path, ...]]:
    """编译并链接一个适合 WinAMS 导入的 ARM ELF。"""
    if not sources:
        raise ValueError("至少需要一个 C 源文件")
    output.parent.mkdir(parents=True, exist_ok=True)
    object_dir = output.parent / f".{output.stem}.objects"
    objects = []
    for index, source in enumerate(sources):
        object_path = object_dir / f"{index:03d}_{source.stem}.o"
        build_object(source, object_path, config, include_dirs, defines)
        objects.append(object_path)

    args = _common_flags(config) + ["-nostdlib", "-Wl,--gc-sections"]
    if entry:
        args.append(f"-Wl,-e,{entry}")
    for symbol in _winams_stub_symbols(sources):
        args.append(f"-Wl,--undefined={symbol}")
    if allow_unresolved:
        args.append("-Wl,--unresolved-symbols=ignore-all")
    args += [str(item) for item in objects] + ["-o", str(output)]
    _run(config, args)
    return output, tuple(objects)


def convert_to_winams_omf(elf: Path, output: Path,
                          converter: str | Path | None = None,
                          dwarf_version: int = 5) -> Path:
    """调用 WinAMS armgccomf，把 GCC ELF 转成 WinAMS `.xlo`。"""
    if dwarf_version not in (4, 5):
        raise ValueError("dwarf_version 必须是 4 或 5")
    tool = find_arm_omf_converter(converter)
    output.parent.mkdir(parents=True, exist_ok=True)
    _run_executable(tool, [f"-gdwarf-{dwarf_version}", str(elf), str(output)])
    return output
