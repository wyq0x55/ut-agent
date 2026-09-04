"""Read-only evidence and generation reporting."""

from .evidence import (
    ArtifactEvidence,
    ElfEvidence,
    MapEvidence,
    MotEvidence,
    XloEvidence,
    analyze_artifacts,
    read_elf,
    read_map,
    read_mot,
    read_xlo,
)
from .corpus import (
    ProjectCorpusManifest,
    STANDARD_GAP_CATEGORIES,
    build_corpus_validation_report,
    compare_function_semantics,
    golden_for_unit,
    load_corpus_manifest,
    normalize_generated_manifest,
    preflight_corpus,
    render_project_validation_markdown,
    validate_corpus_paths,
    write_corpus_validation_report,
    write_project_validation_markdown,
)
from .cases import (
    AMBIGUOUS_MATCH, EXACT_SEMANTIC_MATCH, EQUIVALENT_REPRESENTATIVE,
    EXTRA_GENERATED, MATCH_TYPES, MISSING_GENERATED, PARTIAL_MATCH,
    build_generated_cases, build_golden_cases, match_cases,
    match_semantic_cases, normalize_generated_cases, normalize_golden_cases,
)

__all__ = [
    "ArtifactEvidence", "ElfEvidence", "MapEvidence", "MotEvidence", "XloEvidence",
    "analyze_artifacts", "read_elf", "read_map", "read_mot", "read_xlo",
    "ProjectCorpusManifest", "STANDARD_GAP_CATEGORIES",
    "build_corpus_validation_report", "compare_function_semantics",
    "golden_for_unit", "load_corpus_manifest", "normalize_generated_manifest",
    "preflight_corpus", "render_project_validation_markdown",
    "validate_corpus_paths",
    "write_corpus_validation_report", "write_project_validation_markdown",
    "AMBIGUOUS_MATCH", "EXACT_SEMANTIC_MATCH", "EQUIVALENT_REPRESENTATIVE",
    "EXTRA_GENERATED", "MATCH_TYPES", "MISSING_GENERATED", "PARTIAL_MATCH",
    "build_generated_cases", "build_golden_cases", "match_cases",
    "match_semantic_cases", "normalize_generated_cases", "normalize_golden_cases",
]
