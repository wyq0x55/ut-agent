"""Tests for parallel execution, deterministic ordering, and CLI jobs flag."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ut_agent.cli.parser import build_parser
from ut_agent.observability import ProgressRecorder
from ut_agent.project import load_manifest
from ut_agent.reporting import (
    build_corpus_validation_report,
    load_corpus_manifest,
)
from ut_agent.reporting.corpus import _resolve_validation_workers
from ut_agent.targets.winams.index import _resolve_workers, load_index

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "config" / "projects" / "N-O2608-PSD-087.corpus.json"


def test_resolve_workers_boundaries():
    assert _resolve_workers(1, 10) == 1
    assert _resolve_workers(4, 10) == 4
    assert _resolve_workers(16, 2) == 2  # capped by task_count
    assert _resolve_workers(None, 1) == 1  # 1 task always 1 worker
    assert _resolve_validation_workers(1, 10) == 1
    assert _resolve_validation_workers(4, 10) == 4
    assert _resolve_validation_workers(8, 3) == 3


def test_cli_parser_jobs_option():
    parser = build_parser()

    args_psd = parser.parse_args([
        "psd-project", "dummy.csv",
        "--product-root", ".",
        "--out", ".tmp",
        "-j", "8",
    ])
    assert args_psd.jobs == 8

    args_corpus = parser.parse_args([
        "validate-corpus",
        "--manifest", str(CORPUS),
        "--out", ".tmp",
        "--jobs", "4",
    ])
    assert args_corpus.jobs == 4


def test_progress_recorder_collector_and_record_events(tmp_path: Path):
    events_log = tmp_path / "progress-events.jsonl"
    recorder = ProgressRecorder(events_log, project="TEST_PROJ", run_id="run_1")

    # In-memory worker collector
    worker_events: list[dict] = []
    worker_recorder = ProgressRecorder(
        project="TEST_PROJ", run_id="run_1", collector=worker_events
    )
    with worker_recorder.stage("fn_a", "worker_stage", {"meta": 1}):
        pass

    assert len(worker_events) == 2
    assert worker_events[0]["phase"] == "start"
    assert worker_events[1]["phase"] == "end"
    assert not events_log.exists()  # Worker did not touch disk

    # Main process aggregates
    recorder.record_events(worker_events)
    assert events_log.is_file()
    lines = [json.loads(line) for line in events_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["function"] == "fn_a"
    assert lines[1]["stage"] == "worker_stage"


def test_corpus_validation_report_determinism_across_workers(tmp_path: Path):
    """Ensure jobs=1 and jobs=2 produce identical reports and preserve ordering."""
    manifest = load_corpus_manifest(CORPUS)
    context = load_manifest(manifest.context_manifest)
    rows = load_index(manifest.index_csv, manifest.product_root)

    # Build mock units for 3 rows
    units = []
    for row in rows[:3]:
        out_dir = tmp_path / row.target_rel
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "schema_version": 1, "status": "VALIDATED", "intent_count": 10,
            "csv_intent_count": 10, "validated_intent_count": 10,
            "obligation_kinds": {"branch": 10}, "solve_statuses": {"SAT": 10},
            "evaluation_count": 10, "evaluation_complete_count": 10,
            "issues": [], "input_keys": [], "expected_keys": [], "stub_keys": [],
        }
        (out_dir / "test-intents-summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
        unit = SimpleNamespace(
            row=row,
            status="VALIDATED",
            testcsv=out_dir / "TestCsv" / f"{row.function}.csv",
            intent_manifest=out_dir / "test-intents.json",
        )
        units.append(unit)

    out_serial = tmp_path / "serial"
    out_parallel = tmp_path / "parallel"

    report_serial = build_corpus_validation_report(
        manifest, context, tuple(units), output_root=out_serial, jobs=1
    )
    report_parallel = build_corpus_validation_report(
        manifest, context, tuple(units), output_root=out_parallel, jobs=2
    )

    # Functions ordering and details must match exactly
    assert len(report_serial["functions"]) == len(report_parallel["functions"])
    for f_serial, f_parallel in zip(report_serial["functions"], report_parallel["functions"]):
        assert f_serial["row"] == f_parallel["row"]
        assert f_serial["function"] == f_parallel["function"]
        assert f_serial["comparison"]["equivalence"] == f_parallel["comparison"]["equivalence"]

    assert report_serial["totals"] == report_parallel["totals"]
    assert report_serial["status"] == report_parallel["status"]


def test_generate_single_target_direct(tmp_path: Path):
    from ut_agent.targets.winams.index import _generate_single_target, IndexRow
    from ut_agent.ir.codec import document_to_function_ir
    from tests.test_ir_json import _document

    ir = document_to_function_ir(_document())
    row = IndexRow(
        row_number=1,
        callcnt=0,
        source_name="sample.c",
        function="target",
        source_path=Path(ir.file),
        target_rel=Path("sample.c/target"),
        target_base_rel=Path(""),
    )

    result = _generate_single_target(
        row=row,
        ir=ir,
        output_root=tmp_path / "out",
        project_context=None,
        rule_pack=None,
        call_max=5,
        check_golden=False,
        reference_base=tmp_path,
        project_id="TEST_PROJ",
        run_id="run_1",
    )
    assert result.unit.status in {"VALIDATED", "NEEDS_REVIEW"}
    assert result.unit.testcsv.is_file()
    assert result.unit.stub.is_file()
    assert result.unit.ir_json.is_file()
    assert result.unit.intent_manifest.is_file()
    assert len(result.events) >= 2

