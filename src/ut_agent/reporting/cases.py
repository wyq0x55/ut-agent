"""Target-neutral testcase models for offline corpus validation.

The generation side writes semantic observables to ``test-intents.json``.
This module combines those observables with a reviewed Golden only after
generation has finished.  It never participates in obligation solving or
oracle construction.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from ut_agent.learning.golden import normalize_label, truth_vector


EXACT_SEMANTIC_MATCH = "EXACT_SEMANTIC_MATCH"
EQUIVALENT_REPRESENTATIVE = "EQUIVALENT_REPRESENTATIVE"
PARTIAL_MATCH = "PARTIAL_MATCH"
MISSING_GENERATED = "MISSING_GENERATED"
EXTRA_GENERATED = "EXTRA_GENERATED"
AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"

MATCH_TYPES = (
    EXACT_SEMANTIC_MATCH,
    EQUIVALENT_REPRESENTATIVE,
    PARTIAL_MATCH,
    MISSING_GENERATED,
    EXTRA_GENERATED,
    AMBIGUOUS_MATCH,
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是 object: {path}")
    return value


def _value_class(value: Any) -> str:
    if not isinstance(value, int):
        return "symbol"
    if value == 0:
        return "zero"
    if value == 1:
        return "one"
    if value in (0xFF, 0xFFFF, 0xFFFFFFFF):
        return "type-max"
    if 0x1000 <= value <= 0xFFFF and value % 0x100 == 0:
        return "pointer-address"
    return "literal"


def _classes(values: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): _value_class(value) for key, value in values.items()}


def _compact(value: Any) -> str:
    return "".join(str(value or "").split())


def _key_aliases(key: Any) -> set[str]:
    compact = _compact(key)
    return {
        compact,
        compact.lstrip("@*"),
        compact.split("/")[-1],
        compact.split("/")[-1].lstrip("@*"),
    }


def _alias_index(mapping: Mapping[str, Any]) -> dict[str, tuple[str, Any]]:
    index: dict[str, tuple[str, Any]] = {}
    for actual, value in mapping.items():
        record = (str(actual), value)
        for alias in _key_aliases(actual):
            index.setdefault(alias, record)
    return index


def _lookup(
    mapping: Mapping[str, Any], key: Any,
    index: Mapping[str, tuple[str, Any]] | None = None,
) -> tuple[str | None, Any]:
    wanted = _key_aliases(key)
    indexed = index or _alias_index(mapping)
    for alias in wanted:
        if alias in indexed:
            return indexed[alias]
    return None, None


def _map_difference(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Return deterministic differences, preserving both evidence values."""
    differences: dict[str, Any] = {}
    left_index = _alias_index(left)
    right_index = _alias_index(right)
    keys = sorted({str(key) for key in left} | {str(key) for key in right})
    for key in keys:
        left_key, left_value = _lookup(left, key, left_index)
        right_key, right_value = _lookup(right, key, right_index)
        if left_key is None or right_key is None or left_value != right_value:
            differences[key] = {
                "generated": left_value if left_key is not None else None,
                "golden": right_value if right_key is not None else None,
            }
    return differences


def _required_difference(
    actual: Mapping[str, Any], required: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare only values explicitly required by the reviewed case."""
    differences: dict[str, Any] = {}
    actual_index = _alias_index(actual)
    for key in sorted(str(item) for item in required):
        actual_key, actual_value = _lookup(actual, key, actual_index)
        required_value = required[key]
        if actual_key is None or actual_value != required_value:
            differences[key] = {
                "generated": actual_value if actual_key is not None else None,
                "golden": required_value,
            }
    return differences


def _stable_payload(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": case.get("kind"),
        "label": case.get("label"),
        "outcome": case.get("outcome"),
        "truth_vector": case.get("truth_vector"),
        "identity": case.get("identity", {}),
        "inputs": case.get("inputs", {}),
        "expected": case.get("expected", {}),
        "required_input_values": case.get("required_input_values", {}),
        "required_expected_values": case.get("required_expected_values", {}),
        "stub": case.get("stub", {}),
        "oracle": case.get("oracle", {}),
    }


def _digest(case: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _stable_payload(case), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _assign_stable_ids(cases: Iterable[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    result = [dict(case) for case in cases]
    groups: dict[str, list[dict[str, Any]]] = {}
    for case in result:
        groups.setdefault(_digest(case), []).append(case)
    for digest in sorted(groups):
        members = sorted(
            groups[digest],
            key=lambda item: (
                str(item.get("case_id", "")),
                repr(item.get("inputs", {})),
            ),
        )
        for index, case in enumerate(members, 1):
            case["semantic_id"] = f"{prefix}:{digest}:{index}"
    return result


def _stub_columns(values: Mapping[str, Any]) -> list[str]:
    result = []
    for key in values:
        compact = _compact(key).lower()
        if (compact.startswith("amstb_") or "callcnt" in compact
                or "ptout" in compact or "ptin" in compact
                or "arg" in compact):
            result.append(str(key))
    return sorted(result)


def _generated_viewpoint(kind: str, semantic: Mapping[str, Any]) -> str:
    value = str(semantic.get("viewpoint", ""))
    if value:
        return value
    return {
        "branch": "branch_outcome",
        "condition": "condition_outcome",
        "mcdc": "condition_combination",
        "case": "switch_case",
        "execution": "unlabelled",
        "loop": "unlabelled",
    }.get(kind, kind)


def _identity(
    *, kind: str, viewpoint: str, label: str, outcome: Any,
    semantic: Mapping[str, Any], obligation: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "viewpoint": viewpoint,
        "label": label,
        "outcome": outcome,
        "branch_id": obligation.get("branch_id", semantic.get("branch_id")),
        "condition_index": obligation.get(
            "condition_index", semantic.get("condition_index")
        ),
        "pair_id": obligation.get("pair_id", semantic.get("pair_id")),
        "boundary_class": obligation.get(
            "boundary_class", semantic.get("boundary_class")
        ),
        "boundary_value": obligation.get(
            "boundary_value", semantic.get("boundary_value")
        ),
        "case_label": obligation.get("case_label", semantic.get("case_label")),
        "branch_index": semantic.get("branch_index"),
    }


def normalize_generated_cases(
    source: Path | Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build semantic cases from one generated intent manifest."""
    raw = _read_json(Path(source)) if isinstance(source, (str, Path)) else dict(source)
    intents = raw.get("intents", [])
    if not isinstance(intents, list):
        raise ValueError("generated intents 必须是 array")
    evaluations = {
        str(item.get("obligation_id")): item
        for item in raw.get("evaluations", [])
        if isinstance(item, dict) and item.get("obligation_id") is not None
    }
    result: list[dict[str, Any]] = []
    for index, item in enumerate(intents, 1):
        if not isinstance(item, dict):
            continue
        obligation = item.get("obligation", {})
        if not isinstance(obligation, dict):
            obligation = {}
        semantic = item.get("semantic", {})
        if not isinstance(semantic, dict):
            semantic = {}
        kind = str(obligation.get("kind", semantic.get("kind", "unknown")))
        viewpoint = _generated_viewpoint(kind, semantic)
        label = normalize_label(semantic.get("label"))
        outcome = obligation.get("outcome", semantic.get("outcome"))
        inputs = item.get("inputs", {})
        expected = item.get("expected", {})
        if not isinstance(inputs, dict):
            inputs = {}
        if not isinstance(expected, dict):
            expected = {}
        stub_behavior = item.get("stub_behavior", {})
        if not isinstance(stub_behavior, dict):
            stub_behavior = {}
        stub_keys = sorted(set(_stub_columns(inputs)) | set(stub_behavior))
        stub_values = {
            key: stub_behavior.get(key, inputs.get(key)) for key in stub_keys
        }
        evaluation = evaluations.get(str(obligation.get("oid")), {})
        validation = item.get("validation", {})
        if not isinstance(validation, dict):
            validation = {}
        result.append({
            "case_id": str(item.get("case_id", f"U{index:03d}")),
            "kind": viewpoint,
            "label": label,
            "outcome": outcome,
            "truth_vector": semantic.get("truth_vector"),
            "identity": _identity(
                kind=kind, viewpoint=viewpoint, label=label, outcome=outcome,
                semantic=semantic, obligation=obligation,
            ),
            "inputs": dict(inputs),
            "expected": dict(expected),
            "raw_inputs": dict(item.get("raw_inputs", {}) or {}),
            "raw_expected": dict(item.get("raw_expected", {}) or {}),
            "input_value_classes": _classes(inputs),
            "expected_value_classes": _classes(expected),
            "required_input_values": dict(
                semantic.get("required_input_values", {}) or {}
            ),
            "required_expected_values": dict(
                semantic.get("required_expected_values", {}) or {}
            ),
            "stub": {"columns": stub_keys, "values": stub_values},
            "pre_state": {
                key: value for key, value in inputs.items() if key not in stub_keys
            },
            "oracle": {"columns": sorted(expected), "values": dict(expected)},
            "provenance": {
                "source": "generated FunctionIR suite",
                "baseline_ref": obligation.get("baseline_ref", raw.get("baseline_ref")),
                "rule_id": obligation.get("rule_id", ""),
                "source_fact": obligation.get("source_fact", ""),
                "project_rule_ref": obligation.get("project_rule_ref", ""),
                "project_mcdc_enabled": obligation.get(
                    "project_mcdc_enabled"
                ),
            },
            "obligation_id": str(obligation.get("oid", "")),
            "valid": validation.get("status") == "VALIDATED"
            and not validation.get("errors"),
            "validation": validation,
            "evaluation": evaluation,
        })
    return _assign_stable_ids(result, "generated")


def normalize_golden_cases(
    source: Mapping[str, Any], *, source_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Adapt normalized Golden cases to the common semantic case model."""
    raw_cases = source.get("cases", [])
    if not isinstance(raw_cases, list):
        raise ValueError("Golden cases 必须是 array")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_cases, 1):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "unlabelled"))
        label = normalize_label(raw.get("label"))
        inputs = raw.get("inputs", {})
        expected = raw.get("expected", {})
        if not isinstance(inputs, dict):
            inputs = {}
        if not isinstance(expected, dict):
            expected = {}
        vector = raw.get("truth_vector")
        if vector is None:
            vector = truth_vector(label)
        stub = raw.get("stub", {})
        if not isinstance(stub, dict):
            stub = {}
        oracle = raw.get("oracle", {})
        if not isinstance(oracle, dict):
            oracle = {}
        result.append({
            "case_id": str(raw.get("case_id", f"U{index:03d}")),
            "kind": kind,
            "label": label,
            "outcome": raw.get("outcome"),
            "truth_vector": vector,
            "identity": {
                "kind": kind, "viewpoint": kind, "label": label,
                "outcome": raw.get("outcome"),
                "branch_index": raw.get("branch_index"),
                "case_label": raw.get("case_label"),
            },
            "inputs": dict(inputs),
            "expected": dict(expected),
            "raw_inputs": dict(raw.get("raw_inputs", {}) or {}),
            "raw_expected": dict(raw.get("raw_expected", {}) or {}),
            "input_value_classes": dict(raw.get("input_value_classes", {}) or {}),
            "expected_value_classes": dict(
                raw.get("expected_value_classes", {}) or {}
            ),
            "required_input_values": dict(
                raw.get("required_input_values", {}) or {}
            ),
            "required_expected_values": dict(
                raw.get("required_expected_values", {}) or {}
            ),
            "stub": {
                "columns": list(stub.get("columns", [])),
                "values": dict(stub.get("values", {}) or {}),
            },
            "pre_state": dict(raw.get("pre_state", {}) or {}),
            "oracle": {
                "columns": list(oracle.get("columns", expected)),
                "values": dict(oracle.get("values", expected) or {}),
            },
            "provenance": {
                "source": "reviewed Golden",
                # The artifact path is recorded once in the enclosing
                # function report; repeating it in every case would make
                # fingerprints machine-specific and needlessly inflate the
                # evidence file.
                "artifact": "golden-csv",
            },
            "valid": True,
        })
    return _assign_stable_ids(result, "golden")


def _kind_compatible(generated: Mapping[str, Any], golden: Mapping[str, Any]) -> bool:
    left = str(generated.get("kind", ""))
    right = str(golden.get("kind", ""))
    if left == right:
        return True
    return {
        "condition_combination": {"condition_outcome"},
        "condition_outcome": {"condition_combination"},
        "unlabelled": {"execution", "loop"},
    }.get(right, set()).__contains__(left)


def _truth_equal(left: Any, right: Any) -> bool | None:
    if left is None or right is None:
        return None
    return left == right


def _score(generated: Mapping[str, Any], golden: Mapping[str, Any]) -> int:
    if not _kind_compatible(generated, golden):
        return -10_000
    score = 30
    if generated.get("outcome") is not None and golden.get("outcome") is not None:
        score += 25 if generated.get("outcome") == golden.get("outcome") else -25
    generated_label = str(generated.get("label", ""))
    golden_label = str(golden.get("label", ""))
    if generated_label and golden_label:
        score += 55 if generated_label == golden_label else -25
    truth = _truth_equal(generated.get("truth_vector"), golden.get("truth_vector"))
    if truth is True:
        score += 80
    elif truth is False:
        score -= 60
    if golden.get("kind") == "switch_case":
        score += 35 if generated.get("identity", {}).get("case_label") == \
            golden.get("identity", {}).get("case_label") else -35
    golden_branch = golden.get("identity", {}).get("branch_index")
    generated_branch = generated.get("identity", {}).get("branch_index")
    if golden_branch is not None and generated_branch is not None:
        score += 40 if golden_branch == generated_branch else -20
    required = _required_evidence(generated, golden)
    score += min(
        20, len(required["input_matches"]) + len(required["expected_matches"])
    )
    score -= min(
        30, len(required["input_mismatches"]) +
        len(required["expected_mismatches"])
    )
    generated_stub = generated.get("stub", {})
    golden_stub = golden.get("stub", {})
    if generated_stub.get("columns") == golden_stub.get("columns"):
        score += 10
    elif golden_stub.get("columns"):
        score -= 10
    generated_oracle = generated.get("oracle", {})
    golden_oracle = golden.get("oracle", {})
    if generated_oracle.get("columns") == golden_oracle.get("columns"):
        score += 10
    elif golden_oracle.get("columns"):
        score -= 10
    return score


def _required_evidence(
    generated: Mapping[str, Any], golden: Mapping[str, Any],
) -> dict[str, Any]:
    generated_inputs = generated.get("inputs", {})
    golden_inputs = golden.get("required_input_values", {})
    generated_expected = generated.get("expected", {})
    golden_expected = golden.get("required_expected_values", {})
    input_matches: dict[str, Any] = {}
    expected_matches: dict[str, Any] = {}
    generated_input_index = _alias_index(generated_inputs)
    generated_expected_index = _alias_index(generated_expected)
    for key, value in golden_inputs.items():
        actual_key, actual = _lookup(
            generated_inputs, key, generated_input_index,
        )
        if actual_key is not None and actual == value:
            input_matches[str(key)] = value
    for key, value in golden_expected.items():
        actual_key, actual = _lookup(
            generated_expected, key, generated_expected_index,
        )
        if actual_key is not None and actual == value:
            expected_matches[str(key)] = value
    return {
        "input_matches": input_matches,
        "expected_matches": expected_matches,
        "input_mismatches": _required_difference(generated_inputs, golden_inputs),
        "expected_mismatches": _required_difference(
            generated_expected, golden_expected,
        ),
    }


def _record_evidence(
    generated: Mapping[str, Any], golden: Mapping[str, Any],
) -> dict[str, Any]:
    required = _required_evidence(generated, golden)
    generated_stub = generated.get("stub", {})
    golden_stub = golden.get("stub", {})
    generated_oracle = generated.get("oracle", {})
    golden_oracle = golden.get("oracle", {})
    stub_mismatches = _map_difference(
        generated_stub.get("values", {}), golden_stub.get("values", {})
    )
    oracle_mismatches = _map_difference(
        generated_oracle.get("values", {}), golden_oracle.get("values", {})
    )
    return {
        "viewpoint_equal": _kind_compatible(generated, golden),
        "label_equal": generated.get("label") == golden.get("label"),
        "truth_vector_equal": _truth_equal(
            generated.get("truth_vector"), golden.get("truth_vector")
        ),
        "outcome_equal": (
            None if generated.get("outcome") is None
            or golden.get("outcome") is None
            else generated.get("outcome") == golden.get("outcome")
        ),
        "required_input_matches": required["input_matches"],
        "required_expected_matches": required["expected_matches"],
        "required_input_mismatches": required["input_mismatches"],
        "required_expected_mismatches": required["expected_mismatches"],
        "stub_mismatches": stub_mismatches,
        "oracle_mismatches": oracle_mismatches,
        "free_input_differences": _map_difference(
            generated.get("inputs", {}), golden.get("inputs", {})
        ),
        "free_expected_differences": _map_difference(
            generated.get("expected", {}), golden.get("expected", {})
        ),
    }


def _semantic_identity_equal(
    generated: Mapping[str, Any], golden: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> bool:
    return bool(
        evidence.get("viewpoint_equal")
        and evidence.get("label_equal")
        and evidence.get("truth_vector_equal") is not False
        and evidence.get("outcome_equal") is not False
        and not evidence.get("required_input_mismatches")
        and not evidence.get("required_expected_mismatches")
        and not evidence.get("stub_mismatches")
        and not evidence.get("oracle_mismatches")
    )


def _match_type(
    generated: Mapping[str, Any], golden: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> str:
    if not _semantic_identity_equal(generated, golden, evidence):
        return PARTIAL_MATCH
    if (generated.get("inputs", {}) == golden.get("inputs", {})
            and generated.get("expected", {}) == golden.get("expected", {})):
        return EXACT_SEMANTIC_MATCH
    return EQUIVALENT_REPRESENTATIVE


def _sort_key(case: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(case.get("kind", "")), str(case.get("label", "")),
        str(case.get("semantic_id", case.get("case_id", ""))),
    )


def _case_key(case: Mapping[str, Any]) -> str:
    """Return a stable key for matching and sequence diagnostics."""
    return str(case.get("semantic_id") or case.get("case_id") or "")


def _case_positions(
    cases: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    return {
        _case_key(case): index
        for index, case in enumerate(cases)
        if _case_key(case)
    }


def _structural_label(label: Any) -> str:
    """Normalize presentation-only syntax for physical row-order checks."""
    value = normalize_label(str(label or ""))
    value = re.sub(
        r"0[xX]([0-9A-Fa-f]+)",
        lambda match: str(int(match.group(1), 16)),
        value,
    )
    return re.sub(r"(?<=\d)[uUlL]+\b", "", value)


def _structural_case_key(case: Mapping[str, Any]) -> tuple[Any, ...]:
    """Describe row placement without comparing free input/oracle values."""
    identity = case.get("identity", {})
    if not isinstance(identity, Mapping):
        identity = {}
    truth = case.get("truth_vector")
    try:
        truth_key = json.dumps(
            truth, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        truth_key = repr(truth)
    return (
        str(case.get("kind", "")),
        _structural_label(case.get("label")),
        case.get("outcome"),
        truth_key,
        identity.get("branch_index"),
        identity.get("case_label"),
    )


def match_semantic_cases(
    generated: Iterable[Mapping[str, Any]],
    golden: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Match cases and report the required CSV sequence contract.

    Semantic matching remains order-independent so a useful diagnostic can be
    produced for reordered rows.  The original sequences are retained and
    compared separately; Golden replay requires both ``row_count_equal`` and
    ``row_order_equal``.
    """
    generated_sequence = [dict(item) for item in generated]
    golden_sequence = [dict(item) for item in golden]
    if any(not item.get("semantic_id") for item in generated_sequence):
        generated_sequence = _assign_stable_ids(generated_sequence, "generated")
    if any(not item.get("semantic_id") for item in golden_sequence):
        golden_sequence = _assign_stable_ids(golden_sequence, "golden")
    generated_cases = sorted(generated_sequence, key=_sort_key)
    golden_cases = sorted(golden_sequence, key=_sort_key)
    # An unresolved intent is evidence for solver/evaluator triage, not a
    # projected testcase.  Keep it in the generated count, but do not report
    # it as an EXTRA_GENERATED case.
    matchable_generated = [
        item for item in generated_cases if item.get("valid", True)
    ]
    available = {_case_key(item): item for item in matchable_generated}
    records: list[dict[str, Any]] = []
    for golden_case in golden_cases:
        scored = [
            (_score(case, golden_case), case)
            for case in available.values()
        ]
        candidates = sorted(
            (item for item in scored if item[0] > 0),
            key=lambda item: (-item[0], _sort_key(item[1])),
        )
        if not candidates:
            records.append({
                "match_type": MISSING_GENERATED,
                "golden_case_id": golden_case.get("case_id"),
                "golden_semantic_id": golden_case.get("semantic_id"),
                "generated_case_id": None,
                "generated_semantic_id": None,
                "score": None,
                "evidence": {"golden": golden_case.get("identity", {})},
            })
            continue
        best_score = candidates[0][0]
        tied = [case for score, case in candidates if score == best_score]
        if len(tied) > 1:
            records.append({
                "match_type": AMBIGUOUS_MATCH,
                "golden_case_id": golden_case.get("case_id"),
                "golden_semantic_id": golden_case.get("semantic_id"),
                "generated_case_id": None,
                "generated_semantic_id": None,
                "score": best_score,
                "evidence": {
                    "golden": golden_case.get("identity", {}),
                    "generated_candidates": [
                        case.get("case_id") for case in tied
                    ],
                },
            })
            continue
        generated_case = tied[0]
        available.pop(_case_key(generated_case), None)
        evidence = _record_evidence(generated_case, golden_case)
        records.append({
            "match_type": _match_type(generated_case, golden_case, evidence),
            "golden_case_id": golden_case.get("case_id"),
            "golden_semantic_id": golden_case.get("semantic_id"),
            "generated_case_id": generated_case.get("case_id"),
            "generated_semantic_id": generated_case.get("semantic_id"),
            "score": best_score,
            "evidence": evidence,
            "generated_provenance": generated_case.get("provenance", {}),
        })
    for generated_case in sorted(available.values(), key=_sort_key):
        records.append({
            "match_type": EXTRA_GENERATED,
            "golden_case_id": None,
            "golden_semantic_id": None,
            "generated_case_id": generated_case.get("case_id"),
            "generated_semantic_id": generated_case.get("semantic_id"),
            "score": None,
            "evidence": {
                "generated": generated_case.get("identity", {}),
                "provenance": generated_case.get("provenance", {}),
            },
        })
    counts = Counter(str(item["match_type"]) for item in records)
    generated_positions = _case_positions(generated_sequence)
    golden_positions = _case_positions(golden_sequence)
    matched_positions = [
        (
            golden_positions.get(str(item.get("golden_semantic_id"))),
            generated_positions.get(str(item.get("generated_semantic_id"))),
        )
        for item in records
        if item.get("golden_semantic_id") is not None
        and item.get("generated_semantic_id") is not None
    ]
    matched_positions.sort(key=lambda pair: pair[0] if pair[0] is not None else -1)
    row_count_equal = len(matchable_generated) == len(golden_sequence)
    row_order_equal = bool(
        row_count_equal
        and len(matched_positions) == len(golden_sequence)
        and [pair[1] for pair in matched_positions]
        == list(range(len(golden_sequence)))
    )
    order_mismatches = [
        {
            "golden_index": golden_index,
            "generated_index": generated_index,
        }
        for golden_index, generated_index in matched_positions
        if golden_index != generated_index
    ]
    if not row_order_equal and len(matched_positions) != len(golden_sequence):
        order_mismatches.append({
            "golden_matched": len(matched_positions),
            "golden_expected": len(golden_sequence),
        })
    structural_generated = [
        _structural_case_key(case) for case in generated_sequence
    ]
    structural_golden = [
        _structural_case_key(case) for case in golden_sequence
    ]
    structural_order_equal = structural_generated == structural_golden
    structural_mismatches = [
        {
            "golden_index": index,
            "generated_index": index,
            "golden": structural_golden[index],
            "generated": structural_generated[index],
        }
        for index in range(min(len(structural_generated), len(structural_golden)))
        if structural_generated[index] != structural_golden[index]
    ]
    if len(structural_generated) != len(structural_golden):
        structural_mismatches.append({
            "generated_count": len(structural_generated),
            "golden_count": len(structural_golden),
        })
    return {
        "golden_case_count": len(golden_cases),
        "generated_case_count": len(generated_cases),
        "generated_valid_case_count": len(matchable_generated),
        "row_count_equal": row_count_equal,
        "row_order_equal": row_order_equal,
        "row_order": {"equal": row_order_equal, "mismatches": order_mismatches},
        "structural_row_order_equal": structural_order_equal,
        "structural_row_order": {
            "equal": structural_order_equal,
            "mismatches": structural_mismatches,
        },
        "matched_case_count": sum(
            counts[item] for item in
            (EXACT_SEMANTIC_MATCH, EQUIVALENT_REPRESENTATIVE, PARTIAL_MATCH)
        ),
        "counts": dict(sorted(counts.items())),
        "records": records,
    }


# Descriptive aliases make the reporting contract discoverable to callers.
build_generated_cases = normalize_generated_cases
build_golden_cases = normalize_golden_cases
match_cases = match_semantic_cases


__all__ = [
    "AMBIGUOUS_MATCH", "EXACT_SEMANTIC_MATCH", "EQUIVALENT_REPRESENTATIVE",
    "EXTRA_GENERATED", "MATCH_TYPES", "MISSING_GENERATED", "PARTIAL_MATCH",
    "build_generated_cases", "build_golden_cases", "match_cases",
    "match_semantic_cases", "normalize_generated_cases", "normalize_golden_cases",
]
