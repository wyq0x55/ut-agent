"""Project-level integration tests for the standalone Clang path."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ut_agent.parser import default_clang_extractor
from ut_agent.winams.project import generate_project


def test_project_uses_context_source_for_global_initializer(tmp_path: Path):
    if default_clang_extractor() is None:
        pytest.skip("repository standalone extractor is not built")

    soft = tmp_path / "soft"
    source_dir = soft / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "state.h").write_text(
        "extern const int state;\n", encoding="ascii"
    )
    (source_dir / "target.c").write_text(
        '#include "state.h"\n'
        "void target(void) { if (state == 1) { } }\n",
        encoding="ascii",
    )
    (source_dir / "state.c").write_text(
        '#include "state.h"\n'
        "const int state = 1;\n",
        encoding="ascii",
    )
    manifest = tmp_path / "project.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "clang-project",
                "source": "src/target.c",
                "include_root": "src",
                "call_max": 5,
                "cpu": "cortex-m4",
                "dwarf_version": 5,
                "defines": {},
                "functions": [{"name": "target"}],
            }
        ),
        encoding="utf-8",
    )

    project = generate_project(soft, manifest, tmp_path / "out", build=False)
    unit = project.units[0]
    csv = unit.testcsv.read_bytes().decode("cp932")

    assert unit.generation_status == "VALIDATED"
    assert "state == 1" in csv
    assert unit.intent_manifest is not None and unit.intent_manifest.is_file()
