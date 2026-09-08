# PSD baseline interpretation calibration

This workflow implements Issue #12 without making Golden a generation input.
It applies when a reviewed Golden differs from a generated semantic testcase.

## Required investigation order

1. Confirm that Typed FunctionIR represents the source fact. The first divergence is `FUNCTION_IR_GAP`.
2. Confirm the executable implementation follows the approved runtime baseline. A divergence is `IMPLEMENTATION_DRIFT`.
3. Confirm the runtime baseline maps the approved documentation semantics completely. A divergence is `RUNTIME_MAPPING_DRIFT`.
4. Confirm the documentation interpretation matches the declared raw PSD workbook evidence. A divergence is `BASELINE_INTERPRETATION_DRIFT`.
5. Confirm whether an approved project rule or exception is required (`PROJECT_RULE_GAP`).
6. Recheck the reviewed Golden (`GOLDEN_ERROR`).
7. Only then propose `NORMATIVE_RULE_GAP`; incomplete evidence remains `NEEDS_REVIEW`.

The correction direction is upstream to downstream: raw evidence/document interpretation, runtime mapping, implementation, then generated output. A function name, project name, Golden row, or fixed table index is never a rule condition.

## Stable report contract

`project-validation.json` contains a top-level `calibration` object with the declared workbook evidence, fixed investigation order, and cross-project evidence status. Every gap has a `calibration` object. It records an observed pipeline category but starts as `NEEDS_REVIEW`; this prevents a comparison result from being mistaken for a normative decision.

If a generation gate has failed, its initial finding is marked `CANDIDATE_ROOT`. Per-case Golden matching is recorded as `SKIPPED_GENERATION_GATE` rather than expanded into unadjudicable row symptoms. Totals provide both `root_gap_count` and `derived_gap_count`; derived gaps are reserved for observations made after a valid generation gate.

For any conclusion, retain the observed mismatch, Golden evidence, FunctionIR evidence, raw workbook cell/sheet/revision, source documentation mapping, runtime mapping, implementation evidence, classification, proposed correction layer, cross-project evidence, counterexamples, and a synthetic plus real regression. Absence of any required evidence is `NEEDS_REVIEW`.

## Issue #12 execution plan

The work is executed in dependency order. A later phase never changes an
earlier representation without completing its evidence gate.

1. **Evidence freeze (complete):** record the generator commit and preserve
   existing generated reports as historical evidence. They are not restamped
   as evidence for a newer checkout.
2. **Report and agent contract (complete):** emit the calibration decision
   order, distinguish candidate root findings from derived observations, and
   prohibit Golden replay.
3. **Pilot source-to-runtime review (active):** review one mismatch family
   against raw cells, interpretation, runtime contract and implementation.
   The first pilot is [PSD 4-5 table arrays](baselines/psd-rebuild-v1.6/calibration/4-5-table-array.md).
4. **Runtime correction decision (complete):** if a source-supported
   behavior cannot be represented, create a versioned candidate baseline with
   a semantic representation; do not mutate `psd-rebuild@1.0` or add a
   function-specific engine condition. The approved table-array correction is
   `psd-rebuild@1.1`, now locked by the N-O2608 ProjectManifest.
5. **Downstream propagation (synthetic complete):** generic implementation
   reads the explicit contract; targeted synthetic regression is complete.
6. **Real-corpus validation (deferred):** run the current project and the
   earlier PSD project only when their source, Golden and generated artifacts
   have a matching, recorded provenance. This is intentionally not satisfied
   by historical output from another commit.

The current no-rerun boundary does not permit claiming the real-corpus
regression completion condition.
