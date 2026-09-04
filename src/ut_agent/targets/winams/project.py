"""从 Soft 输入生成自包含的 WinAMS 单元测试工程。

这个模块的源码生成输入是：

* 用户交付的 ``Soft`` 源码树；
* 仓库内的项目 manifest（函数清单和配置宏）；
* 可选的编译器/WinAMS 执行工具。

默认生成不搜索参考工程的 ``winAMS/src``，也不读取 golden TestCsv/DefineVar。
golden 只可通过显式校验选项作为只读对照物。完整项目的 ``Soft.map``、``Soft.mot``、
``Soft.out`` 和 ``Soft.out.xlo`` 属于规则引擎允许引用的证据范围；独立的产物分析
阶段再按显式配置读取它们，用于地址、链接、机器码和 WinAMS 兼容性证据，不替代
源码 AST 的语义分析。
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

from ut_agent.toolchain.arm_gcc import (
    ArmGccConfig,
    build_elf,
    convert_to_winams_omf,
    find_arm_gcc,
)
from ut_agent.toolchain import (
    ClangExtractor,
    default_clang_extractor,
    discover_compile_sources,
    make_compile_context,
)
from ut_agent.generation import generate_intents, generate_suite, load_rule_pack
from ut_agent.project.model import ResolvedProjectContext
from ut_agent.targets.winams import stub as stub_generate
from ut_agent.targets.winams import csv as csv_render
from ut_agent.targets.winams.define_var import (
    entries_from_ir,
    read_define_var,
    render_define_var,
    render_winams_ini,
)


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
    define_var: Path | None = None
    intent_manifest: Path | None = None
    generation_status: str = "UNSUPPORTED"


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


def _wsl_to_windows(path: Path | str) -> str:
    raw = os.fspath(path)
    if raw.startswith("/mnt/") and len(raw) >= 7:
        drive = raw[5]
        rest = raw[7:]
        return f"{drive.upper()}:\\{rest.replace('/', chr(92))}"
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == "/":
        drive = raw[1]
        rest = raw[3:]
        return f"{drive.upper()}:\\{rest.replace('/', chr(92))}"
    text = str(Path(raw).resolve())
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
    """生成最小但完整的 WinAMS 工程配置。

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


def _copy_source_tree(soft_root: Path, include_root: Path, out_root: Path) -> None:
    """复制 Soft 的源码/include 树，生成可独立打开的 winAMS 输入树。"""
    source_root = (soft_root / include_root).resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Soft include 根目录不存在：{source_root}")
    shutil.copytree(source_root, out_root / include_root, dirs_exist_ok=True)


def _direct_includes(source: Path) -> tuple[str, ...]:
    """提取被测 C 文件的直接头文件，供独立 stub TU 使用。"""
    text = source.read_bytes().decode("cp932", errors="replace")
    found = re.findall(r'^\s*#\s*include\s*[<"]([^">]+)[">]', text, re.MULTILINE)
    return tuple(dict.fromkeys(found))


def _winams_mpu_for_cpu(cpu: str) -> str:
    """将 manifest CPU 名映射为 WinAMS 的 MPU 名称。"""
    normalized = cpu.lower().replace("_", "-")
    if normalized in {"rh850", "rh850(ghs)"}:
        return "RH850"
    return {
        "cortex-m0": "Cortex-M0/M1(ARM GCC)",
        "cortex-m1": "Cortex-M0/M1(ARM GCC)",
        "cortex-m3": "CortexM3(GCC)",
        "cortex-m4": "Cortex-M4(ARM GCC soft)",
    }.get(normalized, "Cortex-M4(ARM GCC soft)")


def _normalize_reference_mpu(mpu: str) -> str:
    """把 MPU 信息表标签归一化为 .amsy 实际使用的固定名称。"""
    normalized = mpu.strip().lower()
    if normalized in {"rh850", "rh850(ghs)"}:
        return "RH850"
    return mpu.strip()


def _reference_define_var_path(
    reference_root: Path, source_rel: Path, function: str,
) -> Path:
    """定位原项目对应函数的 IO 登录文件。"""
    return reference_root / "winAMS" / source_rel / function / "DefineVar.dat"


def generate_project(
    soft_root: Path,
    manifest: Path,
    output_root: Path,
    *,
    build: bool = False,
    compiler: str | Path | None = None,
    converter: str | Path | None = None,
    cpu: str | None = None,
    reference_out: str | Path | None = None,
    reference_xlo: str | Path | None = None,
    reference_mpu: str | None = None,
    reference_define_var: str | Path | None = None,
    clang_extractor: str | Path | None = None,
    rules_path: str | Path | None = None,
    project_context: ResolvedProjectContext | None = None,
) -> GeneratedProject:
    """从 Soft 生成全部 manifest 用例；可选编译或引用已有 ELF/xlo。"""
    soft_root = soft_root.resolve()
    output_root = output_root.resolve()
    spec = load_project_spec(manifest.resolve())
    rule_pack = load_rule_pack(Path(rules_path).resolve() if rules_path else None)
    reference_mode = reference_out is not None or reference_xlo is not None
    if reference_mode and build:
        raise ValueError("reference 模式不能同时编译新 ELF/xlo")
    if reference_mode and (reference_out is None or reference_xlo is None):
        raise ValueError("reference 模式必须同时提供 reference_out/reference_xlo")
    reference_elf = Path(reference_out).resolve() if reference_out else None
    reference_object = Path(reference_xlo).resolve() if reference_xlo else None
    reference_define_var_path = (
        Path(reference_define_var).resolve() if reference_define_var else None
    )
    if reference_elf is not None and not reference_elf.is_file():
        raise FileNotFoundError(f"reference .out 不存在：{reference_elf}")
    if reference_object is not None and not reference_object.is_file():
        raise FileNotFoundError(f"reference .xlo 不存在：{reference_object}")
    if reference_define_var_path is not None and not reference_define_var_path.is_file():
        raise FileNotFoundError(
            f"reference DefineVar.dat 不存在：{reference_define_var_path}"
        )
    # WinAMS 的工程容器本身使用 ``<out>/src/.../<源文件>`` 目录布局，
    # 其中 ``Dma.c`` 是目录名而不是源码文件名。源码树因此放到独立的
    # ``<out>/source`` 下，避免 Windows 文件/目录同名冲突，同时保持工程
    # 目录与输入源码完全分离。
    source_root = output_root / "source"
    source = _copy_source(soft_root, spec.source, source_root)
    _copy_source_tree(soft_root, spec.include_root, source_root)
    include_dirs = discover_include_dirs(soft_root, spec.include_root)

    selected_cpu = cpu or spec.cpu
    if build and selected_cpu.lower().startswith("rh850"):
        raise ValueError(
            "当前没有确定的 RH850 编译器；请使用 --no-build 生成源码/测试工程，"
            "编译器方案另行决策"
        )
    gcc_path = find_arm_gcc(compiler) if build else None
    gcc_config = (ArmGccConfig(compiler=gcc_path, cpu=selected_cpu)
                  if gcc_path else None)
    mpu_name = (
        _normalize_reference_mpu(reference_mpu or "RH850")
        if reference_mode else _winams_mpu_for_cpu(selected_cpu)
    )
    if reference_mode and not mpu_name:
        raise ValueError("reference_mpu 不能为空")

    primary_source = (soft_root / spec.source).resolve()
    selected_extractor = (
        Path(clang_extractor).resolve() if clang_extractor
        else default_clang_extractor()
    )
    extractor = ClangExtractor(selected_extractor)
    # Context discovery is filesystem-only.  All semantic facts, including
    # static/global initializers and function-pointer targets, come from the
    # C++ LibTooling extractor over these translation units.
    context_sources = discover_compile_sources(soft_root, primary_source)
    extractor_context = make_compile_context(
        context_sources, include_dirs, spec.defines,
    )
    extracted = extractor.extract_targets(
        extractor_context,
        tuple((primary_source, item.name) for item in spec.functions),
        cwd=primary_source.parent,
    )
    # 兼容此前已经生成的扁平观察目录：旧目录把源文件名落成普通文件，
    # 新布局无法在同一路径创建同名工程目录。只更新旧函数输出，不删除
    # 旧文件；全新 output 仍使用 <out>/src/.../<源文件>/<函数>/。
    legacy_unit_layout = (output_root / spec.source).is_file()
    units: list[GeneratedUnit] = []
    for item in spec.functions:
        ir = extracted[(primary_source, item.name)]
        # 保持原 WinAMS 的工程布局：<out>/src/.../<源文件>/<函数>/。
        # out 本身可以是 work/winAMS，也可以是 .build/ast-only-winams。
        unit_root = output_root if legacy_unit_layout else output_root / spec.source
        unit_dir = unit_root / item.name
        test_dir = unit_dir / "TestCsv"
        output_dir = unit_dir / "Output"
        stub = output_root / "source" / "winams" / item.name / "AMSTB_SrcFile.c"
        testcsv = test_dir / f"{item.name}.csv"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(
            stub_generate.render_stub_c(
                ir, spec.call_max, extra_includes=_direct_includes(source)
            ), encoding="utf-8", newline="\n"
        )
        if project_context is not None:
            suite = generate_suite(ir, project_context)
            generation_document = suite.to_dict()
            generation_status = suite.status
            _write_cp932(testcsv, csv_render.render_suite_csv(
                ir, suite, title=f"{item.name} 単体テスト",
            ))
            csv_intent_count = sum(
                intent.validation.valid for intent in suite.intents
            )
        else:
            generation = generate_intents(ir, rule_pack)
            generation_document = generation.to_dict()
            generation_status = generation.status
            _write_cp932(testcsv, csv_render.render_intents_csv(
                ir, generation,
                title=f"{item.name} 単体テスト",
            ))
            csv_intent_count = len(generation.validated_intents)
        generation_document.update({
            "csv_written": True,
            "csv_kind": (
                "validated" if generation_status == "VALIDATED"
                else "partial_candidate"
            ),
            "csv_intent_count": csv_intent_count,
        })
        intent_manifest = unit_dir / "test-intents.json"
        intent_manifest.parent.mkdir(parents=True, exist_ok=True)
        intent_manifest.write_text(
            json.dumps(generation_document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        elf = xlo = None
        if reference_mode:
            assert reference_elf is not None and reference_object is not None
            elf = reference_elf
            xlo = reference_object
        elif build:
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
        amsy_text = _amsy_text(unit_dir, xlo, elf, stub)
        amsy_text = amsy_text.replace(
            "MpuFixedName=Cortex-M4(ARM GCC soft)",
            f"MpuFixedName={mpu_name}",
        ).replace(
            "MpuModelFixedName=Cortex-M4(ARM GCC soft)",
            f"MpuModelFixedName={mpu_name}",
        )
        if mpu_name == "RH850":
            amsy_text = amsy_text.replace(
                "ToolFixedName=ARM GCC OMF Converter",
                "ToolFixedName=GHS",
            ).replace(
                "FileType=xlo",
                "FileType=x30",
            )
        if reference_mode:
            amsy_text = amsy_text.replace(
                "ToolFixedName=ARM GCC OMF Converter",
                "ToolFixedName=GHS",
            )
            assert reference_elf is not None
            reference_root = reference_elf.parent.parent
            startup = reference_root / "AMS_Setting" / "SS_STARTUP.txt"
            source_path_file = reference_root / "AMS_Setting" / "path.txt"
            caseplayer = (
                reference_root / "CasePlayer2" / "3MLA_MVC" / "3MLA_MVC.vproj"
            )
            original_stub = reference_elf.parent / "src" / "AMSTB_SrcFile.c"
            if startup.is_file():
                amsy_text = amsy_text.replace(
                    "Start=\nSystemGOption=",
                    f"Start={_wsl_to_windows(startup)}\nSystemGOption=",
                )
            if source_path_file.is_file():
                amsy_text = amsy_text.replace(
                    "SrcPathFile=\nSrcPathCnt=0",
                    f"SrcPathFile={_wsl_to_windows(source_path_file)}\n"
                    "SrcPathCnt=0",
                )
            if caseplayer.is_file():
                amsy_text = amsy_text.replace(
                    "Cp2Proj=",
                    f"Cp2Proj={_wsl_to_windows(caseplayer)}",
                )
            if original_stub.is_file():
                amsy_text = amsy_text.replace(
                    f"STB_SRCPATH={_wsl_to_windows(stub)}",
                    f"STB_SRCPATH={_wsl_to_windows(original_stub)}",
                )
            # 原 RH850 工程的 WinAMS 执行区间来自其已验证的 .amsy；
            # reference 模式沿用这组固定的测试区设置，不改变 .out/.xlo。
            amsy_text = amsy_text.replace(
                "MultiRunCount=1", "MultiRunCount=2",
            ).replace(
                "TestAreaStart=0\nTestAreaEnd=0",
                "TestAreaStart=57876\nTestAreaEnd=58367",
            )
            amsy_text = amsy_text.replace(
                "StopAdress=\nStopRadio=0",
                "StopAdress=p_vog_pwom_hook_RsetBoot\nStopRadio=1",
            ).replace(
                "FileType=xlo",
                "FileType=x30",
            )
        _write_cp932(amsy, amsy_text)
        define_var = unit_dir / "DefineVar.dat"
        define_var_source = reference_define_var_path
        if define_var_source is None and reference_mode:
            assert reference_elf is not None
            define_var_source = _reference_define_var_path(
                reference_elf.parent.parent, spec.source, item.name,
            )
        if define_var_source is not None and define_var_source.is_file():
            define_entries = read_define_var(define_var_source)
        else:
            define_entries = entries_from_ir(ir)
        _write_cp932(define_var, render_define_var(define_entries))
        winams_ini = unit_dir / "WinAMS.INI"
        _write_cp932(
            winams_ini,
            render_winams_ini(
                define_var,
                windows_path=_wsl_to_windows(define_var),
            ),
        )
        units.append(GeneratedUnit(
            name=item.name, source=source, stub=stub, testcsv=testcsv,
            expected=item.expected, elf=elf, xlo=xlo, amsy=amsy, output=output_dir,
            define_var=define_var,
            intent_manifest=intent_manifest,
            generation_status=generation_status,
        ))
    return GeneratedProject(output_root, source, tuple(units))


def compare_bytes(actual: Path, expected: Path) -> bool:
    """比较 WinAMS 结果，不做耗时字段归一化。"""
    return actual.is_file() and expected.is_file() and actual.read_bytes() == expected.read_bytes()


def _windows_executable(path: str | Path) -> bool:
    text = str(path).lower()
    return text.endswith(".exe") or (len(text) >= 2 and text[1] == ":")


def _winams_batch_args(unit: GeneratedUnit) -> list[str]:
    """构造 WinAMS 官方批处理参数，工程文件必须放在最后。"""
    xml_result = unit.output / f"{unit.name}.amsyr"
    return [
        "-b",
        "-output", str(unit.output),
        # .amsy 的 InDir=.\TestCsv 是 WinAMS 的 CSV 搜索根；官方样例
        # 传入的是文件名，传绝对路径会出现 CsvList 为空但返回码为 0。
        "-testCsv", unit.testcsv.name,
        "-xmlex", str(xml_result),
        str(unit.amsy),
    ]


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
        # SSTManager 的批处理入口要求工程文件放在最后，并显式给出
        # output/testCsv；仅传 ``-b <amsy>`` 会静默退出而不产生 Output。
        args = _winams_batch_args(unit)
        if _windows_executable(exe):
            native_executable = _wsl_to_windows(exe)
            native_args = [
                _wsl_to_windows(arg) if not arg.startswith("-") else arg
                for arg in args
            ]
            if os.name == "nt":
                # Git Bash 启动的 Windows Python 直接调用 SSTManager.exe，
                # 避免运行过程中隐式切换到 PowerShell。
                command_args = [native_executable, *native_args]
            else:
                ps = shutil.which("powershell.exe")
                if not ps:
                    results.append((unit.name, False, "找不到 powershell.exe"))
                    continue
                def quote(value: str) -> str:
                    return "'" + value.replace("'", "''") + "'"
                command = "& " + quote(native_executable)
                command += " " + " ".join(quote(value) for value in native_args)
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
