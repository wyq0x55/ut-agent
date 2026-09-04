# Runtime baseline approval record

This record is the explicit approval metadata for the runtime
`psd-rebuild@1.0` TestBaseline.

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
