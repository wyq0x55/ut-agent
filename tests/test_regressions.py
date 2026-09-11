"""回归测试：覆盖跨平台执行和值域/退出码边界。"""
from pathlib import Path

from ut_agent import batch, cli
from ut_agent.generation.boundary import control_candidates
from ut_agent.toolchain.process import _wsl_path
from ut_agent.ir import Atom, Branch, CallSite, ControlVar, FunctionIR, Param
from ut_agent.targets.winams.stub import render_stub_c
from ut_agent.toolchain import ClangExtractor, default_clang_extractor, make_compile_context


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


def test_generated_stub_uses_winams_call_contract():
    ir = FunctionIR(name="f", file="f.c", line=1, ret_type="void")
    ir.calls = [CallSite(
        order=0, callee="callee", line=1,
        params=[Param(name="value", type="int")], ret_type="void"
    )]
    source = render_stub_c(ir, call_max=1)
    assert "#define WINAMS_STUB" in source
    assert "#define CALL_MAX  1" in source
    assert "AMSTB_callee" in source
    assert "CALLCNT_callee" in source
    assert "ARG00_callee[ CALL_MAX ]" in source


def test_clang_recovers_multiline_macro_condition_without_comments(tmp_path):
    """多行宏条件的两个原子都应保留，行尾注释不能污染变量名。"""
    source = tmp_path / "multiline.c"
    source.write_text(
        "#define LIMIT 2\n"
        "int sample(int left, int right)\n"
        "{\n"
        "    if ((LIMIT == left)\n"
        "      || (LIMIT == right)) /* branch */\n"
        "    { return 1; }\n"
        "    return 0;\n"
        "}\n",
        encoding="ascii",
    )
    context = make_compile_context([source])
    ir = ClangExtractor(default_clang_extractor()).extract(
        context, "sample", cwd=tmp_path
    )
    assert len(ir.branches) == 1
    branch = ir.branches[0]
    assert branch.connective == "||"
    assert [(atom.var, atom.boundary) for atom in branch.atoms] == [
        ("left", 2), ("right", 2),
    ]


def test_targeted_branch_excludes_descendants_and_uses_param_equality_default():
    """Solve candidate must exclude descendants when setting collateral branches to False,
    and scalar parameters must default to their equality boundary if present."""
    from ut_agent.generation.engine import (
        _descendant_branch_ids,
        _generic_inputs,
        _targeted_branch_candidate,
    )
    from ut_agent.ir import Atom, Branch, ControlVar, FunctionIR, Param

    ir = FunctionIR(name="test_func", file="test.c", line=1, ret_type="int")
    ir.params = [Param(name="mode", type="int")]
    ir.control_vars = [
        ControlVar(name="mode", var="mode", var_type="int", source="param")
    ]
    b1 = Branch(
        bid="b1", parent_bid="", line=1, kind="if", connective=None,
        atoms=[
            Atom(
                var="mode", var_type="int", op="==", boundary=1,
                boundary_name=None, text="mode == 1",
            )
        ],
    )
    b2 = Branch(
        bid="b2", parent_bid="b1", line=2, kind="if", connective=None,
        atoms=[
            Atom(
                var="mode", var_type="int", op="!=", boundary=1,
                boundary_name=None, text="mode != 1",
            )
        ],
    )
    ir.branches = [b1, b2]

    domains, fixed = _generic_inputs(ir)
    assert fixed.get("mode") == 1
    descendants = _descendant_branch_ids(ir, b1)
    assert "b2" in descendants
    cand = _targeted_branch_candidate(ir, domains, fixed, b1, True)
    assert cand is not None
    assert cand["mode"] == 1


def test_local_value_guard_evaluation_avoids_recursion_cycle():
    """Evaluating guards on local value effects must not cause unbounded recursion."""
    from ut_agent.generation.engine import _control_env
    from ut_agent.ir import Atom, Branch, ControlVar, Effect, FunctionIR, TypeInfo

    u8 = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    b0 = Branch(
        bid="b0", kind="if", line=1,
        atoms=[Atom(
            var="flag", var_type="unsigned char", op="==", boundary=1,
            boundary_name=None, text="flag == 1", type_info=u8,
        )],
    )
    b1 = Branch(
        bid="b1", kind="if", parent_bid="b0", line=2,
        atoms=[Atom(
            var="local_v", var_type="unsigned char", op="==", boundary=1,
            boundary_name=None, text="local_v == 1", type_info=u8,
        )],
    )
    ir = FunctionIR(
        name="test_cycle", file="test.c", line=1, ret_type="void",
        branches=[b0, b1],
        control_vars=[
            ControlVar(name="flag", var="flag", source="global", type_info=u8),
            ControlVar(name="local_v", var="local_v", source="local", type_info=u8),
        ],
        local_value_effects=[
            Effect(
                name="local_v", source_offset=100, constant_value=1,
                guards=[{"bid": "b1", "then": True}],
            ),
        ],
    )
    env = _control_env({"flag": 1}, ir)
    assert isinstance(env, dict)
    assert env["flag"] == 1


