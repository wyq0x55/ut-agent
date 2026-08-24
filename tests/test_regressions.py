"""回归测试：覆盖跨平台执行和值域/退出码边界。"""
from pathlib import Path

from ut_agent import batch, cli
from ut_agent.cases.boundary import control_candidates
from ut_agent.host.run import _wsl_path
from ut_agent.ir import Atom, Branch, CallSite, ControlVar, FunctionIR, Param
from ut_agent.stub.generate import render_stub_c


def test_enum_typedef_domain_wins_over_underlying_type():
    ir = FunctionIR(name="f", file="f.c", line=1, ret_type="void")
    ir.enums = {"Mode": {"MODE_A": 0, "MODE_B": 1, "MODE_C": 2}}
    ir.control_vars = [ControlVar(
        name="mode", var="mode", source="param", var_type="Mode"
    )]
    ir.branches = [Branch(
        bid="B01", kind="if", line=1,
        atoms=[Atom(
            var="mode", var_type="unsigned int", op="==", boundary=1,
            boundary_name="MODE_B", text="mode == MODE_B"
        )]
    )]

    candidates = control_candidates(ir)
    assert candidates["mode"]["values"] == {0, 1, 2}


def test_wsl_path_accepts_wsl_and_windows_forms():
    assert _wsl_path(Path("/mnt/c/work/project/file.c")) == "/mnt/c/work/project/file.c"
    assert _wsl_path(Path("C:/work/project/file.c")) == "/mnt/c/work/project/file.c"


def test_batch_cli_returns_failure_for_failed_results(monkeypatch):
    monkeypatch.setattr(
        batch,
        "run_batch",
        lambda *args, **kwargs: [{"function": "f", "status": "FAIL_RUN", "note": "boom"}],
    )
    assert cli.main(["batch", "unused.c"]) == 1


def test_batch_cli_allows_skipped_execution(monkeypatch):
    monkeypatch.setattr(
        batch,
        "run_batch",
        lambda *args, **kwargs: [{"function": "f", "status": "SKIP_EXEC", "note": "large"}],
    )
    assert cli.main(["batch", "unused.c"]) == 0


def test_generated_stub_guards_call_max():
    ir = FunctionIR(name="f", file="f.c", line=1, ret_type="void")
    ir.calls = [CallSite(
        order=0, callee="callee", line=1,
        params=[Param(name="value", type="int")], ret_type="void"
    )]
    source = render_stub_c(ir, call_max=1)
    assert "if (callcnt00 >= CALL_MAX)" in source
