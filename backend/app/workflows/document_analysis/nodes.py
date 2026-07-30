from pathlib import Path

from ...core.config import (
    DEEP_PDF_ANALYSIS_TRIGGER_TOKENS,
    DOCUMENT_STORAGE_DIRECTORY,
)
from ...infrastructure.document_repository import (
    complete_document_analysis_job_from_db,
    count_uncovered_nonblank_document_pages_from_db,
    create_document_node_from_db,
    get_document_by_id_from_db,
    list_document_pages_with_tables_from_db,
    list_document_nodes_by_ids_from_db,
    release_answer_jobs_waiting_for_document_from_db,
    replace_document_extraction_from_db,
    replace_document_nodes_from_db,
    update_document_analysis_job_stage_from_db,
    update_document_status_from_db,
)
from ...schemas.documents import DocumentAnalysisState
from ...utils.chat_context import estimate_tokens
from ...services.document_chunking_service import create_document_evidence_chunks
from ...services.document_embedding_service import create_document_embeddings
from ...services.document_extraction_service import extract_structured_pdf
from ...services.document_hierarchy_service import (
    StoredHierarchyNode,
    build_basic_document_summary,
    build_deep_document_hierarchy,
)


def resolve_document_storage_path(storage_name: str) -> Path:
    storage_directory = Path(DOCUMENT_STORAGE_DIRECTORY).resolve()
    storage_path = (storage_directory / storage_name).resolve()

    if storage_directory not in storage_path.parents:
        raise RuntimeError("Invalid document storage path")

    return storage_path


def load_document_node(state: DocumentAnalysisState) -> dict:
    document = get_document_by_id_from_db(state["document_id"])

    if not document:
        raise RuntimeError("Document no longer exists")

    update_document_status_from_db(document["id"], "running", 5, last_error="")
    update_document_analysis_job_stage_from_db(state["job_id"], "loading")
    return {"storage_name": document["storage_name"]}


def extract_document_node(state: DocumentAnalysisState) -> dict:
    update_document_analysis_job_stage_from_db(state["job_id"], "extracting")
    storage_path = resolve_document_storage_path(state["storage_name"])
    file_bytes = storage_path.read_bytes()
    extracted_document = extract_structured_pdf(file_bytes)
    replace_document_extraction_from_db(state["document_id"], extracted_document)
    update_document_status_from_db(state["document_id"], "running", 25)
    return {
        "extracted_token_count": extracted_document.token_count,
        "page_count": len(extracted_document.pages),
    }


def index_evidence_node(state: DocumentAnalysisState) -> dict:
    update_document_analysis_job_stage_from_db(state["job_id"], "indexing_evidence")
    pages = list_document_pages_with_tables_from_db(state["document_id"])
    evidence_chunks = create_document_evidence_chunks(pages)

    if not evidence_chunks:
        raise RuntimeError("No evidence chunks could be created")

    replace_document_nodes_from_db(state["document_id"])
    embedding_inputs = [chunk.title + "\n" + chunk.content for chunk in evidence_chunks]
    embeddings = create_document_embeddings(embedding_inputs)
    stored_evidence_node_ids: list[int] = []

    for evidence_chunk, embedding in zip(evidence_chunks, embeddings):
        stored_node = create_document_node_from_db(
            document_id=state["document_id"],
            node_type=evidence_chunk.node_type,
            hierarchy_level=0,
            title=evidence_chunk.title,
            content=evidence_chunk.content,
            page_start=evidence_chunk.page_start,
            page_end=evidence_chunk.page_end,
            token_count=evidence_chunk.token_count,
            embedding=embedding,
        )
        stored_evidence_node_ids.append(stored_node["id"])

    update_document_status_from_db(state["document_id"], "running", 50)
    return {"evidence_node_ids": stored_evidence_node_ids}


def choose_analysis_route(state: DocumentAnalysisState) -> str:
    if state["extracted_token_count"] > DEEP_PDF_ANALYSIS_TRIGGER_TOKENS:
        return "deep"

    return "basic"


def convert_state_evidence_nodes(state: DocumentAnalysisState) -> list[StoredHierarchyNode]:
    hierarchy_nodes: list[StoredHierarchyNode] = []
    evidence_nodes = list_document_nodes_by_ids_from_db(
        [state["document_id"]],
        state["evidence_node_ids"],
    )

    for evidence_node in evidence_nodes:
        hierarchy_nodes.append(
            StoredHierarchyNode(
                id=evidence_node["id"],
                node_type=evidence_node["node_type"],
                title=evidence_node["title"],
                content=evidence_node["content"],
                page_start=evidence_node["page_start"],
                page_end=evidence_node["page_end"],
                token_count=evidence_node["token_count"],
                leaf_ids=[evidence_node["id"]],
            )
        )

    return hierarchy_nodes


def build_basic_document_node(state: DocumentAnalysisState) -> dict:
    update_document_analysis_job_stage_from_db(state["job_id"], "building_basic_summary")
    evidence_nodes = convert_state_evidence_nodes(state)
    root_node = build_basic_document_summary(
        state["document_id"],
        evidence_nodes,
        state["page_count"],
    )
    update_document_status_from_db(
        state["document_id"],
        "running",
        85,
        analysis_mode="basic",
    )
    return {"analysis_mode": "basic", "root_summary": root_node.content}


def build_deep_document_node(state: DocumentAnalysisState) -> dict:
    update_document_analysis_job_stage_from_db(state["job_id"], "building_hierarchy")
    evidence_nodes = convert_state_evidence_nodes(state)
    root_node = build_deep_document_hierarchy(
        state["document_id"],
        evidence_nodes,
        state["page_count"],
    )
    update_document_status_from_db(
        state["document_id"],
        "running",
        85,
        analysis_mode="deep",
    )
    return {"analysis_mode": "deep", "root_summary": root_node.content}


def verify_document_node(state: DocumentAnalysisState) -> dict:
    update_document_analysis_job_stage_from_db(state["job_id"], "verifying")
    uncovered_page_count = count_uncovered_nonblank_document_pages_from_db(
        state["document_id"]
    )

    if uncovered_page_count:
        raise RuntimeError(f"{uncovered_page_count} nonblank pages lack evidence")

    if estimate_tokens(state["root_summary"]) < 1:
        raise RuntimeError("Document root summary is empty")

    update_document_status_from_db(
        state["document_id"],
        "ready",
        100,
        analysis_mode=state["analysis_mode"],
        summary=state["root_summary"],
        last_error="",
    )
    complete_document_analysis_job_from_db(state["job_id"])
    release_answer_jobs_waiting_for_document_from_db(state["document_id"])
    return {}
