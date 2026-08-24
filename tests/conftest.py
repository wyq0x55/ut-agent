"""共享测试基建：解析配置与 IR 构建。"""
import os
from pathlib import Path

import pytest

from ut_agent.parser import clang_parser

ROOT = Path(__file__).resolve().parents[1]
CP = Path(os.environ.get("UT_AGENT_CLASSIC_PLATFORM",
                        str(ROOT / "examples" / "classic-platform")))
CFG = ROOT / "examples" / "configs" / "cp_canif"
SRC = CP / "communication" / "CanIf" / "src" / "CanIf.c"
DEFINES = {"CANIF_CHANNEL_CNT": "2"}


def build_tu_ir(function: str = "CanIf_SetPduMode"):
    if not SRC.is_file():
        pytest.skip(
            "缺少外部 Classic Platform 基准源码；请设置 UT_AGENT_CLASSIC_PLATFORM "
            "或准备 examples/classic-platform/"
        )
    includes = [CP / "include", CP / "include" / "generic", CP / "base" / "compiler",
                CP / "drivers" / "CanTrcv", CFG, CFG / "libc_stub"]
    includes += sorted(p for p in CP.rglob("inc") if p.is_dir())
    tu = clang_parser.parse_tu(SRC, includes, DEFINES, strict=False)
    return tu, clang_parser.extract_function(tu, SRC, function, DEFINES)


def build_ir(function: str = "CanIf_SetPduMode"):
    return build_tu_ir(function)[1]


@pytest.fixture(scope="session")
def setpdumode_ir():
    return build_ir()
