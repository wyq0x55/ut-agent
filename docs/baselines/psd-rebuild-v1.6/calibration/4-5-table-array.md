# Calibration record: PSD 4-5 table-array coverage

Status: `REVIEW_REQUIRED` — the runtime mapping drift is corrected by approved
`psd-rebuild@1.1`; cross-project evidence is now recorded, but remaining
Golden differences are not isolated to table-array coverage. The
machine-readable record is [4-5-table-array.yaml](4-5-table-array.yaml).

This is a source-to-runtime calibration record, not a new PSD rule and not a
Golden replay instruction. The original workbook transcription remains
unchanged.

| Representation | Recorded behavior |
| --- | --- |
| PSD raw source | `PSD再構築!D212`: arrays handled as tables are checked at every index. |
| Docs interpretation | `4-5-array-compare.yaml` retains “覆盖表数组索引” in `purpose` and `mapping_note`. |
| Runtime `psd-rebuild@1.0` | `array_policy` only has `fixed_index` and `preserve_other_elements`; it has no table-array index-coverage semantic. |
| Generic implementation | `_coverage_variants` expands every extractor-proven `const_table_field` index, but the behavior is enabled indirectly by `array_policy.fixed_index`. |
| N-O2608-PSD-087 FunctionIR | Final run contains three `const_table_field` families with 77 table indexes. All three are `VALIDATED`; their Golden files are present but `NOT_INSPECTED` because the report matching budget was exhausted. |
| N-O2606-PSD-049 FunctionIR | Current run contains three `const_table_field` families with 73 table indexes. All three are `VALIDATED`, their Golden files parse, and all three comparisons remain `AMBIGUOUS_MATCH`. |

## Decision

The raw source and the current documentation interpretation agree. The first
semantic divergence is therefore the executable runtime representation:
`RUNTIME_MAPPING_DRIFT`.

The approved highest-layer correction is `psd-rebuild@1.1`, with an explicit
array-comparison class for table arrays and an index-coverage value of `all`.
It is not another unrelated boolean; `psd-rebuild@1.0` remains immutable. The
generic engine now depends on that explicit contract for 1.1 while retaining
the established legacy interpretation only for 1.0.

## Evidence limits and next checks

The final reports cover both projects: N-O2608 has 6/6 indexed functions
processed and 6/6 generation-validated, while its three table-array Golden
files remain `NOT_INSPECTED` under the report matching budget. N-O2606 has
48/48 indexed functions processed, 48/48 generation-validated, and all three
table-array Golden files parsed; their comparisons remain `AMBIGUOUS_MATCH`
with suite/oracle/projection differences. The report does not attribute those
differences to the array-policy field.
The cross-project FunctionIR facts establish applicability, not a new
normative rule.

The counterexample is an ordinary dynamic array comparison that is not an
extractor-proven `const_table_field`: it must keep fixed selected-index
coverage, not expand every possible array index. The synthetic table/non-table
regression passes. Current and historical PSD corpus evidence is recorded in
`.tmp/issue12-observability-20260908/issue12-table-array-calibration.json`;
unresolved semantic comparisons remain `NEEDS_REVIEW`.
