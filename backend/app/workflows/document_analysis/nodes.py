from pathlib import Path

from ...core.config import DEEP_PDF_ANALYSIS_TRIGGER_TOKENS, DOCUMENT_STORAGE_DIRECTORY
from ...infrastructure.document_job_repository import complete_document_analysis_job_db, release_answer_jobs_waiting_for_document_db, update_document_analysis_job_stage_db
from ...infrastructure.document_node_repository import count_uncovered_document_tables_from_db, count_uncovered_nonblank_document_pages_from_db, count_unprovenanced_summary_nodes_from_db, create_document_evidence_chunk_db, delete_document_nodes_from_db, document_has_root_summary_node_db, list_document_evidence_chunk_ids_db, list_document_evidence_chunks_by_ids_db
from ...infrastructure.document_repository import get_document_by_id_from_db, combine_document_pages_with_tables, insert_doc_pages_and_tables_db, update_document_status_db
from ...schemas.documents import DocumentAnalysisState
from ...services.document_chunking_service import DocumentEvidenceChunk, create_document_evidence_chunks
from ...services.document_embedding_service import create_final_document_embeddings
from ...services.document_extraction_service import extract_structured_pdf
from ...services.document_hierarchy_service import DocumentEvidenceChunkMeta, build_basic_document_summary, build_deep_document_hierarchy


def resolve_document_storage_path(storage_name: str) -> Path:
    storage_directory = Path(DOCUMENT_STORAGE_DIRECTORY).resolve()
    storage_path = (storage_directory / storage_name).resolve()

    if storage_directory not in storage_path.parents:
        raise RuntimeError("Invalid document storage path")

    return storage_path


def stage_to_percents(stage: str) -> int:
    stage_progress = {"queued": 5, "extracted": 25, "indexed": 50, "summarized": 85}
    return stage_progress.get(stage, 5)


def load_document_info_node(state: DocumentAnalysisState) -> dict:
    document = get_document_by_id_from_db(state["document_id"])

    if not document:
        raise RuntimeError("Document no longer exists")

    resume_stage = state.get("resume_stage", "queued")
    document_evidence_chunk_ids: list[int] = []

    if resume_stage in {"indexed", "summarized"}:
        document_evidence_chunk_ids = list_document_evidence_chunk_ids_db(document["id"])

        if not document_evidence_chunk_ids:
            resume_stage = "extracted"

    has_valid_stored_summary = bool(document["summary"].strip()) and document["analysis_mode"] in {"basic", "deep"}

    if resume_stage == "summarized" and (not has_valid_stored_summary or not document_has_root_summary_node_db(document["id"])):
        resume_stage = "indexed"

    progress_percent = stage_to_percents(resume_stage)
    update_document_status_db(document["id"], "running", progress_percent, last_error="")
    loaded_state = {"storage_name": document["storage_name"], "resume_stage": resume_stage, "extracted_token_count": document["extracted_token_count"] or 0, "page_count": document["page_count"] or 0, "analysis_mode": document["analysis_mode"], "root_summary": document["summary"]}

    if resume_stage in {"indexed", "summarized"}:
        loaded_state["document_evidence_chunk_ids"] = document_evidence_chunk_ids

    return loaded_state


def choose_resume_route(state: DocumentAnalysisState) -> str:
    resume_stage = state.get("resume_stage", "queued")

    if resume_stage == "extracted":
        return "index"

    if resume_stage == "indexed":
        return "analyze"

    if resume_stage == "summarized":
        return "verify"

    return "extract"


def extract_document_pages_and_tables_in_db_node(state: DocumentAnalysisState) -> dict:
    storage_path = resolve_document_storage_path(state["storage_name"])
    file_bytes = storage_path.read_bytes()
    extracted_document = extract_structured_pdf(file_bytes)

    insert_doc_pages_and_tables_db(state["document_id"], extracted_document)
    update_document_analysis_job_stage_db(state["job_id"], "extracted")
    update_document_status_db(state["document_id"], "running", 25)
    return {"resume_stage": "extracted", "extracted_token_count": extracted_document.token_count, "page_count": len(extracted_document.pages)}


def build_document_evidence_chunk_embedding_inputs(document_evidence_chunks: list[DocumentEvidenceChunk]) -> list[str]:
    return [f"{chunk.title}\n{chunk.content}" for chunk in document_evidence_chunks]


def store_document_evidence_chunks(document_id: str, document_evidence_chunks: list[DocumentEvidenceChunk]) -> list[int]:
    embedding_inputs = build_document_evidence_chunk_embedding_inputs(document_evidence_chunks)
    chunk_embeddings = create_final_document_embeddings(embedding_inputs)
    document_evidence_chunk_ids = []

    for document_evidence_chunk, chunk_embedding in zip(document_evidence_chunks, chunk_embeddings, strict=True):
        stored_document_evidence_chunk = create_document_evidence_chunk_db(document_id, document_evidence_chunk.chunk_type, document_evidence_chunk.title, document_evidence_chunk.content, document_evidence_chunk.page_start, document_evidence_chunk.page_end, document_evidence_chunk.token_count, chunk_embedding, document_evidence_chunk.source_table_id)
        document_evidence_chunk_ids.append(stored_document_evidence_chunk["id"])
    return document_evidence_chunk_ids


def process_doc_into_basic_chunks(state: DocumentAnalysisState) -> dict:
    document_id = state["document_id"]
    document_pages = combine_document_pages_with_tables(document_id)
    document_evidence_chunks = create_document_evidence_chunks(document_pages)

    if not document_evidence_chunks:
        raise RuntimeError("No document evidence chunks could be created")

    # Rebuilding chunks also removes their derived summary nodes, preventing duplicate search results.
    delete_document_nodes_from_db(document_id)
    document_evidence_chunk_ids = store_document_evidence_chunks(document_id, document_evidence_chunks)
    update_document_analysis_job_stage_db(state["job_id"], "indexed")
    update_document_status_db(document_id, "running", 50)
    return {"resume_stage": "indexed", "document_evidence_chunk_ids": document_evidence_chunk_ids}


def route_analysis_node(_: DocumentAnalysisState) -> dict:
    return {}


def choose_analysis_route(state: DocumentAnalysisState) -> str:
    if state["extracted_token_count"] > DEEP_PDF_ANALYSIS_TRIGGER_TOKENS:
        return "deep"

    return "basic"


def convert_state_document_evidence_chunks(state: DocumentAnalysisState) -> list[DocumentEvidenceChunkMeta]:
    document_evidence_chunks = list_document_evidence_chunks_by_ids_db([state["document_id"]], state["document_evidence_chunk_ids"])
    DocumentEvidenceChunkMetas: list[DocumentEvidenceChunkMeta] = []

    for document_evidence_chunk in document_evidence_chunks:
        hierarchy_node = DocumentEvidenceChunkMeta(id=document_evidence_chunk["id"], node_type=document_evidence_chunk["node_type"], title=document_evidence_chunk["title"], content=document_evidence_chunk["content"], page_start=document_evidence_chunk["page_start"], page_end=document_evidence_chunk["page_end"], token_count=document_evidence_chunk["token_count"], leaf_ids=[document_evidence_chunk["id"]])
        DocumentEvidenceChunkMetas.append(hierarchy_node)

    return DocumentEvidenceChunkMetas


def build_basic_document_node(state: DocumentAnalysisState) -> dict:
    document_evidence_chunk_metas = convert_state_document_evidence_chunks(state)
    root_node = build_basic_document_summary(state["document_id"], document_evidence_chunk_metas, state["page_count"])

    update_document_status_db(state["document_id"], "running", 85, analysis_mode="basic", summary=root_node.content)
    update_document_analysis_job_stage_db(state["job_id"], "summarized")
    return {"resume_stage": "summarized", "analysis_mode": "basic", "root_summary": root_node.content}


def build_deep_document_node(state: DocumentAnalysisState) -> dict:
    document_evidence_chunk_metas = convert_state_document_evidence_chunks(state)
    root_node = build_deep_document_hierarchy(state["document_id"], document_evidence_chunk_metas, state["page_count"])

    update_document_status_db(state["document_id"], "running", 85, analysis_mode="deep", summary=root_node.content)
    update_document_analysis_job_stage_db(state["job_id"], "summarized")
    return {"resume_stage": "summarized", "analysis_mode": "deep", "root_summary": root_node.content}


def verify_document_node(state: DocumentAnalysisState) -> dict:
    uncovered_page_count = count_uncovered_nonblank_document_pages_from_db(state["document_id"])
    uncovered_table_count = count_uncovered_document_tables_from_db(state["document_id"])
    unprovenanced_summary_count = count_unprovenanced_summary_nodes_from_db(state["document_id"])
    has_root_summary_node = document_has_root_summary_node_db(state["document_id"])

    if uncovered_page_count:
        raise RuntimeError(f"{uncovered_page_count} nonblank pages lack evidence")

    if uncovered_table_count:
        raise RuntimeError(f"{uncovered_table_count} extracted tables lack evidence")

    if unprovenanced_summary_count:
        raise RuntimeError(f"{unprovenanced_summary_count} summary nodes lack evidence provenance")

    if not has_root_summary_node:
        raise RuntimeError("Document root summary node is missing")

    if not state["root_summary"].strip():
        raise RuntimeError("Document root summary is empty")

    update_document_status_db(state["document_id"], "ready", 100, analysis_mode=state["analysis_mode"], summary=state["root_summary"], last_error="")
    complete_document_analysis_job_db(state["job_id"])
    release_answer_jobs_waiting_for_document_db(state["document_id"])
    return {}
