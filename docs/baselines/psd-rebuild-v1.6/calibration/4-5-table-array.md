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
| N-O2608-PSD-087 FunctionIR | Direct recheck contains three `const_table_field` families with 77 table indexes. All three are `VALIDATED`; `p_vol_mem_job_write_ramdf` is exact, while the read and write-data families remain `NEEDS_REVIEW` because of suite/representative-value differences. |
| N-O2606-PSD-049 FunctionIR | Direct recheck contains three `const_table_field` families with 73 table indexes. All three are `VALIDATED` and all three compare as `EXACT_SEMANTIC_MATCH` against their Golden cases. |

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
processed and 6/6 generation-validated; its direct table-array recheck has
one exact family and two unresolved families. N-O2606 has 48/48 indexed
functions processed and 48/48 generation-validated; its three direct
table-array rechecks are exact. The remaining N-O2608 differences are not
attributed to the array-policy field.
The cross-project FunctionIR facts establish applicability, not a new
normative rule.

The counterexample is an ordinary dynamic array comparison that is not an
extractor-proven `const_table_field`: it must keep fixed selected-index
coverage, not expand every possible array index. The synthetic table/non-table
regression passes. The direct cross-project evidence is recorded in
`.tmp/issue12-observability-20260908/issue12-table-array-evidence-v1.json`;
unresolved N-O2608 semantic comparisons remain `NEEDS_REVIEW`.
