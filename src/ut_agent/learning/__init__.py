"""Offline evidence, Golden comparison, and candidate-rule learning."""

from .compress import compress_corpus, compress_corpus_file, compress_corpora
from .compare import compare_testcsv
from .corpus import collect_rule_corpus, discover_include_dirs, discover_samples, infer_source_root
from .golden import (
    label_kind, normalize_golden_csv, normalize_label, parse_golden_csv,
    ordered_semantic_csv_signature, semantic_csv_signature,
)
from .gap import BaselineGap, compare_project_with_gaps, compare_semantic_csv
from .rule_infer import infer_rule_pack

__all__ = [
    "collect_rule_corpus", "compress_corpus", "compress_corpus_file",
    "compress_corpora", "discover_include_dirs", "discover_samples",
    "infer_source_root", "infer_rule_pack", "parse_golden_csv", "compare_testcsv",
    "ordered_semantic_csv_signature", "semantic_csv_signature",
    "normalize_golden_csv", "normalize_label",
    "label_kind",
    "BaselineGap", "compare_project_with_gaps", "compare_semantic_csv",
]
