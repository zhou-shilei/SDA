"""Typed records exchanged by the patent table agent."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field


TableRole = Literal[
    "sequence_reference",
    "sequence_variant",
    "binding_assay_ic50",
    "continuation",
    "other",
    "unknown",
]


class Evidence(BaseModel):
    document_id: str
    table_id: str
    image: str
    page: int | None = None
    row_index: int | None = None
    col_index: int | None = None
    bbox: list[float] = Field(default_factory=list)
    raw_text: str = ""
    source: str = "ocr"
    confidence: float | None = None


class DocumentEvidence(BaseModel):
    """A source-PDF text span used to explain or reference a table."""

    evidence_id: str
    page: int
    bbox: list[float] = Field(default_factory=list)
    text: str
    source: Literal["pdf_text", "page_ocr"] = "pdf_text"
    relation: Literal[
        "caption",
        "preceding_paragraph",
        "following_paragraph",
        "footnote",
        "explicit_reference",
        "implicit_reference",
    ]
    confidence: float = 0.0


class TableContext(BaseModel):
    """Coordinate-backed prose associated with one physical table crop."""

    table_id: str
    image: str = ""
    page: int | None = None
    bbox: list[float] = Field(default_factory=list)
    table_number: str | None = None
    logical_table_id: str
    caption: DocumentEvidence | None = None
    nearby_context: list[DocumentEvidence] = Field(default_factory=list)
    referring_sentences: list[DocumentEvidence] = Field(default_factory=list)
    footnotes: list[DocumentEvidence] = Field(default_factory=list)
    continuation_of: str | None = None
    continuation_confidence: float | None = None
    context_image: str | None = None
    context_status: Literal["linked", "review", "missing"] = "missing"


class TableCandidate(BaseModel):
    table_id: str
    image: str
    image_path: str
    page: int | None = None
    bbox: list[float] = Field(default_factory=list)
    crop_confidence: float | None = None
    ocr_text: str = ""
    logical_table_id: str | None = None
    table_number: str | None = None
    roles: list[TableRole] = Field(default_factory=list)
    selection_score: float = 0.0
    selected: bool = False
    reasons: list[str] = Field(default_factory=list)
    status: Literal["candidate", "selected", "rejected", "review"] = "candidate"


class SequenceCandidate(BaseModel):
    document_id: str
    table_id: str
    image: str
    page: int | None = None
    row_index: int
    col_index: int
    raw_text: str
    normalized_sequence: str = ""
    chain: Literal["VH", "VL", "unknown"] = "unknown"
    length: int = 0
    valid_alphabet: bool = False
    invalid_characters: list[str] = Field(default_factory=list)
    validation_status: Literal["accepted", "review", "rejected"] = "review"
    evidence: list[Evidence] = Field(default_factory=list)


class ReviewTask(BaseModel):
    task_id: str
    kind: Literal[
        "table_selection",
        "table_classification",
        "table_context",
        "sequence",
        "construct_pair",
        "assay_link",
        "template",
        "quality_gate",
        "record_validation",
        "ocr",
    ]
    severity: Literal["low", "medium", "high"]
    message: str
    table_id: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    status: Literal["open", "accepted", "rejected"] = "open"


class AgentConfig(BaseModel):
    document_id: str
    input_dir: str
    output_dir: str
    sda_input_file: str | None = None
    source_pdf: str | None = None
    existing_ocr_json: str | None = None
    existing_context_dir: str | None = None
    antigen_target: str | None = None
    keyword_library_dir: str | None = None
    semantic_provider: Literal["codex", "rules"] = Field(default_factory=lambda: os.environ.get("DATASETAGENT_SEMANTIC_PROVIDER", "codex"))
    llm_model: str = Field(default_factory=lambda: os.environ.get("DATASETAGENT_LLM_MODEL", "gpt-6.1-sol"))
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(default_factory=lambda: os.environ.get("DATASETAGENT_LLM_EFFORT", "xhigh"))
    llm_batch_size: int = Field(default=4, ge=1, le=8)
    classification_triage: bool = True
    llm_workers: int = Field(default_factory=lambda: int(os.environ.get("DATASETAGENT_LLM_WORKERS", "6")), ge=1, le=8, validate_default=True)
    sequence_workers: int = Field(default_factory=lambda: int(os.environ.get("DATASETAGENT_SEQUENCE_WORKERS", "3")), ge=1, le=8, validate_default=True)
    extract_sequences: bool = True
    question_answer_enabled: bool = True
    evaluation_enabled: bool = True
    max_answer_questions: int = Field(default=12, ge=1, le=64)
    llm_timeout: int = Field(default=600, ge=10, le=3600)
    max_repair_rounds: int = Field(default=2, ge=0, le=3)
    max_repair_actions: int = Field(default=2, ge=0, le=6)
    backend: Literal["auto", "paddleocr", "tesseract"] = "auto"
    lang: str = "en"
    device: str | None = None
    max_side: int = 2200
    ocr_workers: int = Field(default=1, ge=1, le=128)
    ocr_threads_per_worker: int = Field(default=2, ge=1, le=128)
    ocr_python: str | None = None
    ocr_device: str | None = None
    ocr_rec_batch_size: int | None = Field(default=None, ge=1, le=512)
    ocr_cpu_fallback_workers: int = Field(default=0, ge=0, le=128)
    selection_mode: Literal["heuristic", "all"] = "heuristic"
    target_roles: list[TableRole] = Field(
        default_factory=lambda: [
            "sequence_reference",
            "sequence_variant",
            "binding_assay_ic50",
        ]
    )
    selection_threshold: float = 0.45
    run_ocr: bool = False
    run_sequence_page_ocr: bool = True
    page_index_enabled: bool = Field(default_factory=lambda: os.environ.get("DATASETAGENT_PAGE_INDEX_ENABLED", "1").lower() not in {"0", "false", "no"})
    page_index_workers: int = Field(default_factory=lambda: int(os.environ.get("DATASETAGENT_PAGE_INDEX_WORKERS", "6")), ge=1, le=16, validate_default=True)
    page_index_max_side: int = Field(default=1600, ge=500, le=3000)
    page_detail_max_side: int = Field(default=2400, ge=1600, le=5000)
    page_detail_limit: int = Field(default=32, ge=1, le=5000)
    copy_images: bool = True
    codex_decisions: str | None = None
    markdown_name: str | None = None
    reference_dataset: str | None = None
    reference_entities_only: bool = False


class AgentStateModel(BaseModel):
    run_id: str
    config: AgentConfig
    candidates: list[TableCandidate] = Field(default_factory=list)
    selected_table_ids: list[str] = Field(default_factory=list)
    document_blocks: list[dict] = Field(default_factory=list)
    table_contexts: list[TableContext] = Field(default_factory=list)
    context_errors: list[str] = Field(default_factory=list)
    ocr_results: list[dict] = Field(default_factory=list)
    table_classifications: list[dict] = Field(default_factory=list)
    llm_runs: list[dict] = Field(default_factory=list)
    assay_method_pages: list[dict] = Field(default_factory=list)
    document_index: dict = Field(default_factory=dict)
    page_retrieval: dict = Field(default_factory=dict)
    sequence_reference_passages: list[dict] = Field(default_factory=list)
    document_structure: dict = Field(default_factory=dict)
    sequence_extraction: dict = Field(default_factory=dict)
    question_review: dict = Field(default_factory=dict)
    sequence_evaluation: dict = Field(default_factory=dict)
    sequences: list[SequenceCandidate] = Field(default_factory=list)
    resolved_sequences: list[dict] = Field(default_factory=list)
    document_template: dict = Field(default_factory=dict)
    template_validation: dict = Field(default_factory=dict)
    quality_warnings: list[dict] = Field(default_factory=list)
    construct_validations: list[dict] = Field(default_factory=list)
    dataset_records: list[dict] = Field(default_factory=list)
    record_verification: dict = Field(default_factory=dict)
    repair_plan: dict = Field(default_factory=dict)
    repair_state: dict = Field(default_factory=dict)
    repair_history: list[dict] = Field(default_factory=list)
    review_tasks: list[ReviewTask] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
