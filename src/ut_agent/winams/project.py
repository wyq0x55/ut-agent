"""从 Soft 输入生成自包含的 WinAMS 单元测试工程。

这个模块的输入只有：

* 用户交付的 ``Soft`` 源码树；
* 仓库内的项目 manifest；
* 仓库内已经确认过的 TestCsv/Output golden 契约。

参考工程的 ``winAMS/src`` 不在这里出现，也不会在运行时被搜索。golden
中的 TestCsv 是测试向量契约（包含人工选择的地址、stub 行和不可达分支
标记），不能从 C 源码唯一推导，所以必须作为项目的一部分保存。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ut_agent.host.arm_gcc import (
    ArmGccConfig,
    build_elf,
    convert_to_winams_omf,
    find_arm_gcc,
)
from ut_agent.parser import clang_parser
from ut_agent.stub import generate as stub_generate
from ut_agent.winams import csv_render


@dataclass(frozen=True)
class TestSpec:
    name: str
    testcsv: Path | None
    expected: Path | None


@dataclass(frozen=True)
class ProjectSpec:
    name: str
    source: Path
    include_root: Path
    functions: tuple[TestSpec, ...]
    defines: dict[str, str]
    call_max: int
    cpu: str
    dwarf_version: int


@dataclass(frozen=True)
class GeneratedUnit:
    name: str
    source: Path
    stub: Path
    testcsv: Path
    expected: Path | None
    elf: Path | None
    xlo: Path | None
    amsy: Path
    output: Path


@dataclass(frozen=True)
class GeneratedProject:
    root: Path
    source: Path
    units: tuple[GeneratedUnit, ...]


def load_project_spec(path: Path) -> ProjectSpec:
    """读取 UTF-8 manifest，并把仓库相对路径解析为绝对路径。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent.resolve()

    def resolve(value: str | None) -> Path | None:
        if value is None or value == "":
            return None
        item = Path(value)
        return item if item.is_absolute() else (base / item).resolve()

    # source/include_root 是 Soft 根目录下的相对路径；不能在这里按
    # manifest 所在目录解析，否则运行时会错误地依赖仓库外的参考工程。
    source = Path(raw["source"])
    include_root = Path(raw.get("include_root", "."))
    if source.is_absolute() or include_root.is_absolute():
        raise ValueError("manifest 的 source/include_root 必须是 Soft 内相对路径")
    if not source or not include_root:
        raise ValueError(f"manifest 缺少 source/include_root：{path}")
    functions = tuple(
        TestSpec(
            name=item["name"],
            testcsv=resolve(item.get("testcsv")),
            expected=resolve(item.get("expected")),
        )
        for item in raw["functions"]
    )
    if not functions:
        raise ValueError(f"manifest 没有 functions：{path}")
    defines = {str(k): str(v) for k, v in raw.get("defines", {}).items()}
    return ProjectSpec(
        name=str(raw.get("name", path.stem)),
        source=source,
        include_root=include_root,
        functions=functions,
        defines=defines,
        call_max=int(raw.get("call_max", 5)),
        cpu=str(raw.get("cpu", "cortex-m4")),
        dwarf_version=int(raw.get("dwarf_version", 5)),
    )


def discover_include_dirs(soft_root: Path, include_root: Path) -> tuple[Path, ...]:
    """只从 Soft 内部发现 include 目录，顺序固定。"""
    root = (soft_root / include_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Soft include 根目录不存在：{root}")
    dirs = {root}
    dirs.update(item for item in root.rglob("*") if item.is_dir())
    return tuple(sorted(dirs, key=lambda item: item.as_posix().lower()))


def _wsl_to_windows(path: Path) -> str:
    text = str(path.resolve())
    if len(text) >= 2 and text[1] == ":":
        return text.replace("/", "\\")
    wslpath = shutil.which("wslpath")
    if wslpath:
        result = subprocess.run(
            [wslpath, "-w", text], check=True, capture_output=True, text=True
        )
        return result.stdout.strip()
    return text.replace("/", "\\")


def _write_cp932(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").replace("\r", "\n")
                     .replace("\n", "\r\n").encode("cp932"))


def _amsy_text(unit_dir: Path, xlo: Path, elf: Path, stub: Path) -> str:
    """生成最小但完整的 ARM GCC WinAMS 工程配置。

    ``ObjectFile`` 指向已转换的 xlo；``OMF/InFile`` 保留 ELF 路径，便于
    WinAMS GUI/CLI 重新转换或检查工程。测试运行时仍使用显式 TestCsv。
    """
    current = _wsl_to_windows(unit_dir)
    object_file = _wsl_to_windows(xlo)
    elf_file = _wsl_to_windows(elf)
    stub_file = _wsl_to_windows(stub)
    return f"""[System_G]\nCurrentFolder={current}\nObjectFile={object_file}\nStart=\nSystemGOption=\nAddPath=\nCMD=0\nAutoStart=1\nAutoEnd=1\nRunEachCsv=0\nMergeLogFile=0\nSymLogFile=systemg.log\nRunMultiCsv=0\nMultiRunCount=1\nFastExeMode=0\nNotOutputFuncMsg=0\nExecTestObject=1\nExecSamplCodeObject=0\nSetStackArea=0\nStackAreaStart=0\nStackAreaEnd=0\nSetStackPointer=0\nStackPointer=0\nTestCsvFile=\nSetClock=0\nClock=1.0\nCover=1\nCsvCover=1\nStopAdress=\nStopRadio=0\nCp2Proj=\n[OMF]\nInFile={elf_file}\nLongOmfFileName=1\nFileType=xlo\nPutWrn=0\nSrcPathFile=\nSrcPathCnt=0\nDbgPathFile=\nDbgFileCnt=0\nCopyObj=\nChangeOption=\nAutoCnv=0\n[Base]\nUseOMF=1\nCPP=0\nCPPCVTPROJ=2\nSimEngine=2\n[ProjectView]\nMPUName=\nTypeName=\nMpuFixedName=Cortex-M4(ARM GCC soft)\nToolFixedName=ARM GCC OMF Converter\nMode1FixedName=\nMode2FixedName=\nOptionFixedName=\nMpuModelFixedName=Cortex-M4(ARM GCC soft)\nComLineCheck=0\n[TEST]\nUseAllTestCsvFile=1\nInDir=.\\TestCsv\nOutDir=.\\Output\nShowHideCtrl=1\nAutoCovLog=1\nCoverLogFormat=0\nInitOffset=0\nTestDataTime=1\nLogViewToOutCsvDir=0\nLogViewToOutCsvDirSet=0\nTestArea=0\nTestAreaStart=0\nTestAreaEnd=0\nTestEucCode=0\nC1Cover=1\nMCDCCover=1\nFuncCover=0\nFuncCallCover=0\nC1EnableFor=1\nPutTestTime=1\nPutLineNo=0\nInvOutNoComp=0\nPutReportCsv=0\nRelativeTopCsvPath=0\nOutputToolVersion=0\nOutputToolVersion_COVLOG=0\nShowAllNoCovFuncs=0\nMCDC_switch=0\nMCDC_range_for=0\nMCDC_tf=0\nReviceC0=0\nDATA_COV_MAX_ON=0\nDATA_COV_MAX=1000\nFuncCall_EnableStub=0\nFuncCall_SubZero_100=0\nOutputFuncCovCSV=0\nFuncCovCSVFileNameType=0\nFuncCall_ReportStyle=0\nCOV_ALL_SUBFUNC=0\nCovFraction=0\nAllMemAlloc=1\nCodeMemAlloc=0\nShowSubCsv=0\nActiveTestPs=0\nFocusSrcName=\nClsMemShowMode=0\nSrcPathCvtNo=0\nNoSetPath=0\nNoCheckToOK=1\nOutDataHex=0\nOutDataAry=0\nOutDataAryUpIdx=0\nTestTimeUnit=0\nOutDataAccent=0\nCheckFloatTolerance=0\nDataFormatCheck=0\nTestCaseTable_TF_OK=0\nAutoExe_Func=0\n[Output]\nOutputTestHistory=0\nOutputUnexeFuncList=0\nOutputTargetAndSubCsvResult=0\nTestHistoryFolder=.\\TestHistory\nTestSummary=\nOutputTestCsvEvidence=0\nTestCsvInfo=0\nTestCoverageInfo=0\nStubInfo=0\nVersionInfo=0\nInputAnalysisTable=0\nOutputAnalysisTable=0\nTestCaseTable=0\nIODataAnalysisTable=0\nFlowChart=0\n[TESTCSV_ID]\nAuto_CSVID=0\nCSVID_TYPE=0\nPrefix=\nUseFuncName=1\nFuncName_Len=10\nSeparator=-\nNumber_of_Digits=3\nStartNo=1\n[STUB]\nSTB_PREFIX=AMSTB_\nSTB_SRCPATH={stub_file}\nNewStub=1\nActiveStubPs=0\nSTB_MODE=0\nSTB_IN_OUT=0\nSTB_ARY_IN_OUT=0\nSTB_ARYSIZE_MACRO_PREFIX=AMCALLMAX_\nSTB_ARYSIZE=30\nSTB_ARY_DEF_PUTFILE=0\nSTB_ARY_DEF_FILENAME=AMSTB_ArySizeDef.h\nSTB_COUNTER=0\nSTB_COUNTER_NAME=AM_count\nSTB_PUT_INCLUDE=0\nSTUB_INC_FILE_COUNT=0\nSTB_ACCESS_SPECIFIER=0\nSTB_DO_NOT_PUT_IFDEF_WINAMS_STUB=0\nLAST_STB_SRCPATH=\nLAST_CREATE_STUB_TIME=\n[OTHER]\nTabIndex=3\nSelectCsv=0\nResDirExist=1\nTabSize=4\nUseOutCsvEditor=0\nUseIDE=0\nAutoBild=0\nIDE_ProjPath=\nIDE_ProjFileExps=\nBuildCmdPath=\nBuildParam=\"\"\nSrcEditor=\nSrcOpenParam=\"\"\nCodeKind=0\n"""


def _copy_source(soft_root: Path, source_rel: Path, out_root: Path) -> Path:
    source = (soft_root / source_rel).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Soft 源文件不存在：{source}")
    # manifest 的 source 本身通常以 ``src/`` 开头；输出工程保留该相对
    # 布局，避免出现 ``src/src/...``，也让 DWARF 中的路径直接落到工程。
    destination = out_root / source_rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _direct_includes(source: Path) -> tuple[str, ...]:
    """提取被测 C 文件的直接头文件，供独立 stub TU 使用。"""
    text = source.read_bytes().decode("cp932", errors="replace")
    found = re.findall(r'^\s*#\s*include\s*[<"]([^">]+)[">]', text, re.MULTILINE)
    return tuple(dict.fromkeys(found))


def generate_project(
    soft_root: Path,
    manifest: Path,
    output_root: Path,
    *,
    build: bool = False,
    compiler: str | Path | None = None,
    converter: str | Path | None = None,
) -> GeneratedProject:
    """从 Soft 生成全部 manifest 用例；可选编译为 ELF/xlo。"""
    soft_root = soft_root.resolve()
    output_root = output_root.resolve()
    spec = load_project_spec(manifest.resolve())
    source = _copy_source(soft_root, spec.source, output_root)
    include_dirs = discover_include_dirs(soft_root, spec.include_root)
    tu = clang_parser.parse_tu(source, include_dirs, spec.defines, strict=False)

    gcc_path = find_arm_gcc(compiler) if build else None
    gcc_config = ArmGccConfig(compiler=gcc_path, cpu=spec.cpu) if gcc_path else None
    units: list[GeneratedUnit] = []
    for item in spec.functions:
        ir = clang_parser.extract_function(tu, source, item.name, spec.defines)
        unit_dir = output_root / item.name
        test_dir = unit_dir / "TestCsv"
        output_dir = unit_dir / "Output"
        stub = output_root / "src" / "winams" / item.name / "AMSTB_SrcFile.c"
        testcsv = test_dir / f"{item.name}.csv"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(
            stub_generate.render_stub_c(
                ir, spec.call_max, extra_includes=_direct_includes(source)
            ), encoding="utf-8", newline="\n"
        )
        reference = item.testcsv
        csv_text = csv_render.render_csv(
            ir,
            source_label=item.name,
            title=f"{item.name} 単体テスト",
            reference_csv=reference,
        )
        _write_cp932(testcsv, csv_text)

        elf = xlo = None
        if build:
            assert gcc_config is not None
            elf = unit_dir / f"{item.name}.out"
            xlo = unit_dir / f"{item.name}.xlo"
            build_elf(
                [source, stub], elf, gcc_config, include_dirs=include_dirs,
                defines=spec.defines, entry=item.name, allow_unresolved=True,
            )
            convert_to_winams_omf(elf, xlo, converter, spec.dwarf_version)
        else:
            # 工程文件在 --build=false 时仍然可生成，稍后可由调用者补产物。
            elf = unit_dir / f"{item.name}.out"
            xlo = unit_dir / f"{item.name}.xlo"
        amsy = unit_dir / f"{item.name}.amsy"
        _write_cp932(amsy, _amsy_text(unit_dir, xlo, elf, stub))
        units.append(GeneratedUnit(
            name=item.name, source=source, stub=stub, testcsv=testcsv,
            expected=item.expected, elf=elf, xlo=xlo, amsy=amsy, output=output_dir,
        ))
    return GeneratedProject(output_root, source, tuple(units))


def compare_bytes(actual: Path, expected: Path) -> bool:
    """比较 WinAMS 结果，不做耗时字段归一化。"""
    return actual.is_file() and expected.is_file() and actual.read_bytes() == expected.read_bytes()


def compare_testcsv(project: GeneratedProject) -> list[tuple[str, bool]]:
    """验证生成的输入 CSV 与本地 TestCsv golden 完全一致。"""
    result = []
    for unit in project.units:
        if unit.expected is None:
            result.append((unit.name, True))
            continue
        # expected CSV 的路径由 manifest 的 testcsv 保存在生成前无法从 unit 直接
        # 得到，因此对有 expected 的用例按相邻的 TestCsv 路径约定校验。
        golden = unit.expected.parent / "TestCsv.csv"
        result.append((unit.name, compare_bytes(unit.testcsv, golden)))
    return result


def _windows_executable(path: str | Path) -> bool:
    text = str(path).lower()
    return text.endswith(".exe") or (len(text) >= 2 and text[1] == ":")


def run_winams(
    project: GeneratedProject,
    executable: str | Path = "/mnt/c/WinAMS/BIN/SSTManager.exe",
    *,
    timeout: float = 120.0,
) -> list[tuple[str, bool, str]]:
    """运行有 expected 的单元，并做原始 Output 字节比对。"""
    exe = str(executable)
    results: list[tuple[str, bool, str]] = []
    for unit in project.units:
        if unit.expected is None:
            continue
        unit.output.mkdir(parents=True, exist_ok=True)
        args = [
            "-b", "-output", str(unit.output), "-testCsv", str(unit.testcsv),
            str(unit.amsy),
        ]
        if _windows_executable(exe):
            ps = shutil.which("powershell.exe")
            if not ps:
                results.append((unit.name, False, "找不到 powershell.exe"))
                continue
            def quote(value: str) -> str:
                return "'" + value.replace("'", "''") + "'"
            command = "& " + quote(_wsl_to_windows(Path(exe)))
            command += " " + " ".join(
                quote(_wsl_to_windows(Path(arg))) if not arg.startswith("-") else quote(arg)
                for arg in args
            )
            command_args = [ps, "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            command_args = [exe, *args]
        try:
            proc = subprocess.run(
                command_args, cwd=unit.amsy.parent, timeout=timeout,
                check=False, capture_output=True, text=True,
                encoding="cp932", errors="replace",
            )
        except subprocess.TimeoutExpired:
            results.append((unit.name, False, "WinAMS 超时"))
            continue
        candidates = [unit.output / f"{unit.name}.csv"]
        candidates.extend(sorted(unit.output.glob("*.csv")))
        actual = next((item for item in candidates if item.is_file()), candidates[0])
        ok = proc.returncode == 0 and compare_bytes(actual, unit.expected)
        detail = f"returncode={proc.returncode} actual={actual}"
        if proc.stderr:
            detail += f" stderr={proc.stderr[-300:]}"
        results.append((unit.name, ok, detail))
    return results
