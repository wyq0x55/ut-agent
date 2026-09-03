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
    validate_corpus_paths,
    write_corpus_validation_report,
)

__all__ = [
    "ArtifactEvidence", "ElfEvidence", "MapEvidence", "MotEvidence", "XloEvidence",
    "analyze_artifacts", "read_elf", "read_map", "read_mot", "read_xlo",
    "ProjectCorpusManifest", "STANDARD_GAP_CATEGORIES",
    "build_corpus_validation_report", "compare_function_semantics",
    "golden_for_unit", "load_corpus_manifest", "normalize_generated_manifest",
    "validate_corpus_paths", "write_corpus_validation_report",
]
