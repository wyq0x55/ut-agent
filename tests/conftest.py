"""共享测试基建：解析配置与 IR 构建。"""
import os
from pathlib import Path

import pytest

from ut_agent.parser import (
    ClangExtractor,
    default_clang_extractor,
    make_compile_context,
)

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
    context = make_compile_context([SRC], includes, DEFINES)
    ir = ClangExtractor(default_clang_extractor()).extract(
        context, function, cwd=SRC.parent
    )
    # Preserve the fixture's historical tuple shape for host tests.  The
    # The first value is deliberately not a Python translation-unit object.
    return None, ir


def build_ir(function: str = "CanIf_SetPduMode"):
    return build_tu_ir(function)[1]


@pytest.fixture(scope="session")
def setpdumode_ir():
    return build_ir()
