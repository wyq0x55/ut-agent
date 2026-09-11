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


def test_pointer_dereference_lookup_and_evaluation_does_not_decay_to_address():
    """Pointer dereference '*ptr' must resolve pointee data, not pointer address."""
    from ut_agent.generation.engine import _eval_expression_tree, _lookup
    from ut_agent.ir import FunctionIR

    env = {
        "ptr": 1,
        "ptr[0]": 255,
        "*ptr": 255,
        "@ptr[0]": 255,
    }
    # _lookup must find 255, not decay to 'ptr' (1)
    assert _lookup(env, "*ptr") == 255

    ir = FunctionIR(name="test_deref", file="test.c", line=1, ret_type="void")
    tree = {
        "kind": "unary",
        "op": "*",
        "operand": {"kind": "reference", "name": "ptr"},
    }
    assert _eval_expression_tree(ir, tree, env) == 255


def test_global_array_element_control_var_mapping():
    """Local control derived from global_array_element must map to array element input."""
    from ut_agent.generation.boundary import control_candidates
    from ut_agent.generation.engine import (
        _control_env,
        _domain_key_for,
        _generic_inputs,
        _remap_derived_candidates,
    )
    from ut_agent.generation.semantic import global_key
    from ut_agent.ir import Atom, Branch, ControlVar, FunctionIR, GlobalObject, TypeInfo

    u8 = TypeInfo(
        canonical_type="unsigned char", kind="integer", bit_width=8,
        signed=False, min_value=0, max_value=255,
    )
    b0 = Branch(
        bid="b0", kind="if", line=1,
        atoms=[Atom(
            var="flag_local", var_type="unsigned char", op="==", boundary=1,
            boundary_name="U1G_DAT_ABNORMAL", text="1 == flag_local", type_info=u8,
        )],
    )
    cv = ControlVar(
        name="flag_local", var="flag_local", source="local", type_info=u8,
        branch_ids=["b0"],
        value_origin={
            "kind": "global_array_element",
            "base": "g_flags",
            "index": "idx",
        },
    )
    g_obj = GlobalObject(
        name="g_flags", read=True, write=False, array_sizes=[1],
    )
    ir = FunctionIR(
        name="test_global_arr", file="test.c", line=1, ret_type="void",
        branches=[b0],
        control_vars=[cv],
        global_objects=[g_obj],
    )
    candidates = control_candidates(ir)
    _remap_derived_candidates(ir, candidates)
    g_key = global_key("g_flags", (0,))
    assert g_key in candidates

    domains, fixed = _generic_inputs(ir)
    assert g_key in domains
    key = _domain_key_for(ir, "flag_local", domains)
    assert key in {g_key, "g_flags[0]"}

    env = _control_env({g_key: 1}, ir)
    assert env["flag_local"] == 1


def test_branch_target_atom_alternatives_for_and_false():
    """Conjunctive branch requires testing all alternative children for False."""
    from ut_agent.generation.engine import _branch_target_atom_alternatives
    from ut_agent.ir import Atom, Branch, TypeInfo

    u8 = TypeInfo(canonical_type="unsigned char", kind="integer", bit_width=8)
    b = Branch(
        bid="b0", kind="if", connective="&&", line=1,
        atoms=[
            Atom(var="a", var_type="unsigned char", op="==", boundary=1, boundary_name=None, text="a == 1", type_info=u8),
            Atom(var="b", var_type="unsigned char", op="==", boundary=1, boundary_name=None, text="b == 1", type_info=u8),
        ],
        condition_tree={
            "kind": "logical",
            "op": "&&",
            "children": [
                {"kind": "atom", "index": 0},
                {"kind": "atom", "index": 1},
            ],
        },
    )
    alts = _branch_target_atom_alternatives(b, False)
    assert len(alts) == 2
    assert [(0, False)] in alts
    assert [(1, False)] in alts


def test_stub_return_assignment_preserves_observable_pre_state_argument():
    """When a global is updated via 'g = stub(g)', its argument must resolve to pre-state, not post-state."""
    from ut_agent.generation.engine import (
        _generic_inputs,
        _resolve_call_param_value,
    )
    from ut_agent.ir import CallSite, Effect, FunctionIR, GlobalObject, Param, TypeInfo

    u8 = TypeInfo(canonical_type="unsigned char", kind="integer", bit_width=8, signed=False, min_value=0, max_value=255)
    call = CallSite(
        order=1, callee="calc_stub", line=10, ret_type="u1", return_used=True,
        params=[Param(name="arg", type="u1", type_info=u8)],
        extensions={
            "call_capacity": 1,
            "caller_param_origins": {
                "0": {"kind": "global", "driver": "g_cnt"},
            },
        },
    )
    g_obj = GlobalObject(name="g_cnt", read=True, write=True)
    write_eff = Effect(
        name=None, path="g_cnt", source_offset=100, operator="=",
        origin={"kind": "stub_return", "callee": "calc_stub", "call_offset": 120},
    )
    ir = FunctionIR(
        name="test_fn", file="test.c", line=1, ret_type="void",
        calls=[call],
        global_objects=[g_obj],
        global_write_effects=[write_eff],
        globals_used=["g_cnt"],
    )
    domains, fixed = _generic_inputs(ir)
    assert fixed.get("g_cnt") == 128
    assert fixed.get("call:calc_stub:return:0") == 255

    env = dict(fixed)
    arg_val = _resolve_call_param_value(
        ir, call.params[0], {"kind": "global", "driver": "g_cnt"}, env, (120, 150),
    )
    assert arg_val == 128


def test_normalize_label_handles_circled_digits_and_extension_prefix():
    """Japanese circled digits and 拡張(...) prefix must normalize to canonical branch label."""
    from ut_agent.learning.golden import normalize_label

    assert normalize_label("拡張(FALSE①)") == "FALSE"
    assert normalize_label("拡張(FALSE②)") == "FALSE"
    assert normalize_label("組合せ(TRUE(1))") == "TRUE"
    assert normalize_label("FALSE①") == "FALSE"


def test_derive_obligations_preserves_boundaries_for_global_array_element_locals():
    """A local derived from global_array_element must retain typed status boundary points."""
    from ut_agent.baseline import load_baseline
    from ut_agent.generation.obligation import derive_obligations
    from ut_agent.ir import Atom, Branch, ControlVar, Effect, FunctionIR, GlobalObject, TypeInfo

    baseline = load_baseline("config/baselines/psd-rebuild/1.1.yaml")
    u8 = TypeInfo(canonical_type="unsigned char", kind="integer", bit_width=8, signed=False, min_value=0, max_value=255)
    b0 = Branch(
        bid="b0", kind="if", line=1,
        atoms=[Atom(var="flag", var_type="u1", op="==", boundary=1, boundary_name="U1G_DAT_ON", text="1 == flag", type_info=u8)],
    )
    cv = ControlVar(
        name="flag", var="flag", source="local", type_info=u8, branch_ids=["b0"],
        value_origin={"kind": "global_array_element", "base": "g_arr", "index": "idx"},
    )
    eff = Effect(name="flag", constant_value=0, source_offset=10, operator="=")
    g_obj = GlobalObject(name="g_arr", read=True, write=False, array_sizes=[1])
    ir = FunctionIR(
        name="test_fn", file="test.c", line=1, ret_type="void",
        branches=[b0], control_vars=[cv], global_objects=[g_obj],
        local_value_effects=[eff], globals_used=["g_arr"],
    )
    obligations = derive_obligations(ir, baseline)
    boundary_points = {o.boundary_value for o in obligations if o.kind == "boundary"}
    # Must retain boundary points (e.g. 2, 255) rather than being filtered to only {0}
    assert 2 in boundary_points
    assert 255 in boundary_points


def test_repeated_or_variants_generates_multi_variable_boundary_variants():
    """Multi-variable equality OR chain must emit boundary/endpoint variants across inputs on FALSE outcome."""
    from ut_agent.baseline import load_baseline
    from ut_agent.generation.engine import _repeated_or_variants
    from ut_agent.generation.model import TestObligation
    from ut_agent.ir import Atom, Branch, ControlVar, FunctionIR, TypeInfo

    baseline = load_baseline("config/baselines/psd-rebuild/1.1.yaml")
    u8 = TypeInfo(canonical_type="unsigned char", kind="integer", bit_width=8, signed=False, min_value=0, max_value=255)
    atoms = [
        Atom(var="sw1", var_type="u1", op="==", boundary=3, boundary_name="ON_ON", text="3 == sw1", type_info=u8),
        Atom(var="sw2", var_type="u1", op="==", boundary=3, boundary_name="ON_ON", text="3 == sw2", type_info=u8),
    ]
    branch = Branch(bid="b0", kind="if", line=1, connective="||", atoms=atoms)
    cv1 = ControlVar(name="sw1", var="sw1", source="global", type_info=u8, branch_ids=["b0"])
    cv2 = ControlVar(name="sw2", var="sw2", source="global", type_info=u8, branch_ids=["b0"])
    ir = FunctionIR(
        name="test_fn", file="test.c", line=1, ret_type="void",
        branches=[branch], control_vars=[cv1, cv2], globals_used=["sw1", "sw2"],
    )
    ob_false = TestObligation(
        rule_id="psd.4.compare", source_fact="branch:b0", oid="b0:mcdc:1:F",
        kind="mcdc", branch_id="b0", outcome=False, condition_index=1,
    )
    assignment = {"sw1": 0, "sw2": 0}
    variants = _repeated_or_variants(ir, baseline, ob_false, assignment)
    # Target values outside {3} for boundary 3: 2 (3-1), 4 (3+1), 255 (max)
    assert len(variants) == 4
    assert variants[0] == {"sw1": 0, "sw2": 0}
    assert variants[1] == {"sw1": 2, "sw2": 2}
    assert variants[2] == {"sw1": 4, "sw2": 4}
    assert variants[3] == {"sw1": 255, "sw2": 255}


def test_render_intent_value_preserves_numeric_ptrout():
    """PTROUT columns with numeric values must render as decimal numbers, not pointer addresses."""
    from ut_agent.targets.winams.csv import _render_intent_value
    from ut_agent.ir import FunctionIR

    ir = FunctionIR(name="test_fn", file="test.c", line=1, ret_type="void")
    comment = "AMSTB_SrcFile.c/AMSTB_stub@PTROUT00_stub[0]"
    rendered = _render_intent_value(255, comment=comment, ir=ir)
    assert rendered in ("255", "0xff")
    assert rendered != "0x5400"
    rendered_zero = _render_intent_value(0, comment=comment, ir=ir)
    assert rendered_zero in ("0", "0x0")
    assert rendered_zero != "0x5400"


def test_targeted_branch_candidate_preserves_downstream_true_for_loop():
    """When targeting a loop branch, downstream branches without external calls should prefer TRUE."""
    from ut_agent.generation.engine import _targeted_branch_candidate
    from ut_agent.ir import Atom, Branch, ControlVar, FunctionIR, Provenance, SourceLocation, TypeInfo

    u8 = TypeInfo(canonical_type="unsigned char", kind="integer", bit_width=8, signed=False, min_value=0, max_value=255)
    sl_for = SourceLocation("test.c", 1, 1, 10, 20)
    sl_if = SourceLocation("test.c", 2, 1, 30, 40)
    loc_for = Provenance(spelling=sl_for, expansion=sl_for)
    loc_if = Provenance(spelling=sl_if, expansion=sl_if)
    b_for = Branch(bid="b_for", kind="for", line=1, atoms=[Atom(var="idx", var_type="u1", op="<", boundary=1, boundary_name="1", text="idx < 1", type_info=u8)], provenance=loc_for)
    b_if = Branch(bid="b_if", kind="if", line=2, atoms=[Atom(var="flag", var_type="u1", op="==", boundary=1, boundary_name="1", text="flag == 1", type_info=u8)], provenance=loc_if)
    cv_idx = ControlVar(name="idx", var="idx", source="local", type_info=u8, branch_ids=["b_for"])
    cv_flag = ControlVar(name="flag", var="flag", source="global", type_info=u8, branch_ids=["b_if"])
    ir = FunctionIR(name="test_fn", file="test.c", line=1, ret_type="void", branches=[b_for, b_if], control_vars=[cv_idx, cv_flag], globals_used=["flag"])
    domains = {"idx": [0], "flag": [0, 1]}
    fixed = {"idx": 0, "flag": 0}
    cand = _targeted_branch_candidate(ir, domains, fixed, b_for, True)
    assert cand is not None
    # Downstream if branch flag==1 should be targeted to TRUE (flag=1)
    assert cand.get("flag") == 1


def test_local_self_assignment_evaluates_previous_offset_value():
    """A self-updating local assignment (e.g. x = -x) must resolve previous value before the assignment."""
    from ut_agent.generation.engine import _local_value
    from ut_agent.ir import Effect, FunctionIR, ValueOrigin

    tree = {"kind": "unary", "op": "-", "operand": {"kind": "reference", "name": "val"}}
    eff1 = Effect(name="val", constant_value=-127, source_offset=10)
    eff2 = Effect(name="val", value="-val", source_offset=20, origin=ValueOrigin(kind="local", expression_tree=tree))
    ir = FunctionIR(name="fn", file="test.c", line=1, ret_type="int", local_value_effects=[eff1, eff2])
    res = _local_value(ir, "val", {})
    assert res == 127









