"""M2.5：host 编译执行器（Windows 主控 + WSL gcc）。

Runner 抽象的第一实现 HostRunner：写单 TU → wsl gcc 编译 → 运行 → 收结果。
RealRunner（WinAMS）/ MockRunner（回放）后续在此接口上扩展。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


def _wsl_path(win_path: Path) -> str:
    """Return a path consumable by gcc in WSL.

    The project can be invoked from either Windows Python or WSL Python.  In
    the latter case ``Path.resolve()`` already returns ``/mnt/c/...`` and
    blindly prepending ``/mnt/`` produces the invalid ``/mnt//mnt/c/...``.
    """
    raw = str(win_path).replace("\\", "/")
    if raw.startswith("/"):
        return raw
    match = re.match(r"^([A-Za-z]):/(.*)$", raw)
    if match:
        return f"/mnt/{match.group(1).lower()}/{match.group(2)}"
    resolved = str(Path(raw).expanduser().resolve()).replace("\\", "/")
    if resolved != raw:
        return _wsl_path(Path(resolved))
    raise ValueError(f"无法转换为 WSL 路径: {win_path}")


def compile_and_run(c_file: Path, out_dir: Path, include_dirs=(), defines=None,
                    timeout_compile: int = 120, timeout_run: int = 60) -> list[str]:
    """gcc 编译并执行，返回 stdout 行列表（每用例一行）。

    Windows 主控优先通过 ``wsl.exe`` 执行；在 WSL/Linux 中直接调用
    gcc，避免依赖嵌套的 ``wsl.exe``。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = out_dir / "harness"
    use_wsl = shutil.which("wsl.exe") is not None
    args = ["gcc", "-std=c99", "-w", "-O0"]
    for d in include_dirs:
        args += ["-I", _wsl_path(Path(d)) if use_wsl else str(Path(d))]
    for k, v in (defines or {}).items():
        args.append(f"-D{k}={v}" if v != "" else f"-D{k}")
    args += ["-o", _wsl_path(exe) if use_wsl else str(exe),
             _wsl_path(c_file) if use_wsl else str(c_file)]
    command = ["wsl.exe", "-e", *args] if use_wsl else args
    r = subprocess.run(command, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=timeout_compile)
    if r.returncode != 0:
        raise RuntimeError(f"gcc 编译失败:\n{r.stderr[:2000]}")
    run_command = (["wsl.exe", "-e", _wsl_path(exe)] if use_wsl
                   else [str(exe)])
    r = subprocess.run(run_command, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=timeout_run)
    if r.returncode != 0:
        raise RuntimeError(f"harness 执行失败 rc={r.returncode}:\n{r.stderr[:2000]}")
    return [line for line in r.stdout.splitlines() if line.strip()]


def parse_result_lines(lines: list[str]) -> list[dict]:
    """行格式: callcnt,moduleId,instanceId,apiId,errorId,ret,after（'-' = 未调用）。"""
    out = []
    for line in lines:
        parts = line.split(",")
        callcnt = int(parts[0])
        rec = {"callcnt": callcnt}
        rest = parts[1:]
        rec["args"] = [int(x) if x != "-" else None for x in rest[:-2]]
        rec["ret"] = int(rest[-2])
        rec["after"] = int(rest[-1])
        out.append(rec)
    return out
