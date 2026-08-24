"""M2.5：host 编译执行器（Windows 主控 + WSL gcc）。

Runner 抽象的第一实现 HostRunner：写单 TU → wsl gcc 编译 → 运行 → 收结果。
RealRunner（WinAMS）/ MockRunner（回放）后续在此接口上扩展。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _wsl_path(win_path: Path) -> str:
    s = str(win_path.resolve()).replace("\\", "/")
    drive, _, rest = s.partition(":")
    return f"/mnt/{drive.lower()}/{rest}"


def compile_and_run(c_file: Path, out_dir: Path, include_dirs=(), defines=None,
                    timeout_compile: int = 120, timeout_run: int = 60) -> list[str]:
    """gcc 编译（WSL）并执行，返回 stdout 行列表（每用例一行）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = out_dir / "harness"
    args = ["gcc", "-std=c99", "-w", "-O0"]
    for d in include_dirs:
        args += ["-I", _wsl_path(Path(d))]
    for k, v in (defines or {}).items():
        args.append(f"-D{k}={v}" if v != "" else f"-D{k}")
    args += ["-o", _wsl_path(exe), _wsl_path(c_file)]
    r = subprocess.run(["wsl.exe", "-e", *args], capture_output=True,
                       text=True, timeout=timeout_compile)
    if r.returncode != 0:
        raise RuntimeError(f"gcc 编译失败:\n{r.stderr[:2000]}")
    r = subprocess.run(["wsl.exe", "-e", _wsl_path(exe)], capture_output=True,
                       text=True, timeout=timeout_run)
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
