# Runtime baseline approval record

This record contains the explicit approval metadata for versioned runtime
TestBaseline decisions.

## psd-rebuild@1.1

- Decision: `approved`
- Authority: `repository-owner`
- Approved by: `wyq0x55` (wan37 approval in the Issue #12 execution)
- Approved at: `2026-09-04`
- Scope: `config/baselines/psd-rebuild/1.1.yaml`, specifically the explicit
  PSD 4-5 table-array `index_coverage: all` runtime contract.

The approval is based on the [4-5 calibration record](calibration/4-5-table-array.yaml).
It does not alter the raw Excel cells or promote unrelated source-only sections.

## psd-rebuild@1.0

- Decision: `approved`
- Authority: `repository-owner`
- Approved by: `wyq0x55`
- Approved at: `2026-09-03`
- Scope: the baseline and all eight source-mapped rules in
  `config/baselines/psd-rebuild/1.0.yaml`

## Decision basis

The runtime baseline preserves the source mapping and policy boundaries from
the Ver.1.6 source evidence. The source manifest, section files, and original
workbook remain the traceability inputs; project-level MC/DC remains in the
project manifest rather than in the baseline identity.

This approval is an explicit decision for the current runtime mapping. It does
not rewrite the source evidence status: `docs/baselines/psd-rebuild-v1.6/manifest.yaml`
continues to identify the transcription as `source_only` and `needs_review`.
It also does not claim that generated suites have been executed in WinAMS.

## Evidence

- [Source manifest](manifest.yaml)
- [Baseline index](index.md)
- [Runtime baseline](../../../config/baselines/psd-rebuild/1.0.yaml)
- [Runtime baseline 1.1](../../../config/baselines/psd-rebuild/1.1.yaml)
