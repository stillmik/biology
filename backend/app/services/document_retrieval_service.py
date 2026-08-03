from collections import defaultdict

from ..core.config import MAX_DOCUMENT_EVIDENCE_TOKENS, XAI_MODEL
from ..infrastructure.document_node_repository import list_document_evidence_chunks_from_db, list_document_nodes_by_ids_db, list_document_nodes_by_lexical_search_from_db, list_document_nodes_by_vector_from_db, list_neighboring_document_evidence_chunks_from_db, list_previous_answer_source_ids_from_db
from .document_embedding_service import create_document_query_embedding
from .model_service import generate_model_response

RETRIEVAL_RESULT_LIMIT = 30
RECIPROCAL_RANK_CONSTANT = 60
DEPTH_CLASSIFICATION_OUTPUT_TOKENS = 8


def classify_answer_depth_deterministically(question: str) -> str | None:
    normalized_question = question.casefold()
    evidence_phrases = ("exact value", "exact number", "according to the table", "which table", "what unit", "page-level")
    overview_phrases = ("briefly", "overall", "overview", "summarize the document", "main conclusion")
    section_phrases = ("method", "methodology", "results", "limitations", "conclusion", "population", "study design", "safety", "efficacy")
    deeper_phrases = ("go deeper", "more detail", "in depth", "elaborate")

    if any(phrase in normalized_question for phrase in evidence_phrases):
        return "evidence"

    if any(phrase in normalized_question for phrase in deeper_phrases):
        return "evidence"

    if any(phrase in normalized_question for phrase in overview_phrases):
        return "overview"

    if any(phrase in normalized_question for phrase in section_phrases):
        return "section"

    return None


def classify_answer_depth(question: str) -> str:
    deterministic_depth = classify_answer_depth_deterministically(question)

    if deterministic_depth:
        return deterministic_depth

    system_prompt = "Classify the requested scientific answer depth. Return exactly one word: overview, section, focused, or evidence."
    user_prompt = f"Question:\n{question}"
    model_input = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]

    try:
        classification = generate_model_response(model_input, model=XAI_MODEL, max_output_tokens=DEPTH_CLASSIFICATION_OUTPUT_TOKENS, operation="document_answer_depth_classification")
    except Exception:
        return "focused" if len(question.split()) <= 8 else "section"

    normalized_classification = classification.strip().casefold()

    if normalized_classification in {"overview", "section", "focused", "evidence"}:
        return normalized_classification

    return "focused" if len(question.split()) <= 8 else "section"


def increase_answer_depth(previous_depth: str | None) -> str:
    depth_order = ["overview", "section", "focused", "evidence"]
    current_depth = previous_depth if previous_depth in depth_order else "focused"
    current_index = depth_order.index(current_depth)
    next_index = min(current_index + 1, len(depth_order) - 1)
    return depth_order[next_index]


def reciprocal_rank_fusion(result_lists: list[list[dict]]) -> list[dict]:
    scores: defaultdict[int, float] = defaultdict(float)
    nodes_by_id: dict[int, dict] = {}

    for result_list in result_lists:
        for rank, node in enumerate(result_list, start=1):
            node_id = int(node["id"])
            nodes_by_id[node_id] = node
            scores[node_id] += 1 / (RECIPROCAL_RANK_CONSTANT + rank)

    ranked_node_ids = sorted(scores, key=scores.get, reverse=True)
    fused_results: list[dict] = []

    for node_id in ranked_node_ids:
        node = dict(nodes_by_id[node_id])
        node["fused_score"] = scores[node_id]
        fused_results.append(node)

    return fused_results


def diversify_ranked_nodes(nodes: list[dict], document_ids: list[str]) -> list[dict]:
    selected_nodes: list[dict] = []
    selected_ids: set[int] = set()
    page_counts: defaultdict[tuple[str, int], int] = defaultdict(int)

    for document_id in document_ids:
        first_document_node = None

        for node in nodes:
            if node["document_id"] == document_id:
                first_document_node = node
                break

        if first_document_node:
            selected_nodes.append(first_document_node)
            selected_ids.add(first_document_node["id"])
            page_key = (document_id, first_document_node["page_start"])
            page_counts[page_key] += 1

    for node in nodes:
        if node["id"] in selected_ids:
            continue

        page_key = (node["document_id"], node["page_start"])

        if page_counts[page_key] >= 3:
            continue

        selected_nodes.append(node)
        selected_ids.add(node["id"])
        page_counts[page_key] += 1

    return selected_nodes


def expand_sources_with_neighboring_document_evidence_chunks(sources: list[dict], maximum_seed_count: int = 8) -> list[dict]:
    expanded_sources = list(sources)
    included_source_ids = {source["id"] for source in sources}

    for source in sources[:maximum_seed_count]:
        neighboring_document_evidence_chunks = list_neighboring_document_evidence_chunks_from_db(source["document_id"], source["page_start"], source["page_end"])

        for document_evidence_chunk in neighboring_document_evidence_chunks:
            if document_evidence_chunk["id"] in included_source_ids:
                continue

            expanded_sources.append(document_evidence_chunk)
            included_source_ids.add(document_evidence_chunk["id"])

    return expanded_sources


def limit_document_source_tokens(sources: list[dict]) -> list[dict]:
    selected_sources: list[dict] = []
    used_tokens = 0

    for source in sources:
        projected_tokens = used_tokens + int(source["token_count"]) + 30

        if projected_tokens > MAX_DOCUMENT_EVIDENCE_TOKENS:
            continue

        selected_sources.append(source)
        used_tokens = projected_tokens

    return selected_sources


def retrieve_document_evidence(question: str, documents: list[dict], conversation_id: int, answer_depth: str) -> list[dict]:
    if len(documents) == 1 and documents[0]["analysis_mode"] == "basic":
        return list_document_evidence_chunks_from_db(documents[0]["id"])

    document_ids = [document["id"] for document in documents]
    query_embedding = create_document_query_embedding(question)
    summary_types = ["root", "extralarge", "large", "medium", "small", "overview", "major", "section", "packet"]
    document_evidence_chunk_types = ["evidence", "table"]
    result_lists = [list_document_nodes_by_vector_from_db(document_ids, summary_types, query_embedding, RETRIEVAL_RESULT_LIMIT), list_document_nodes_by_lexical_search_from_db(document_ids, summary_types, question, RETRIEVAL_RESULT_LIMIT), list_document_nodes_by_vector_from_db(document_ids, document_evidence_chunk_types, query_embedding, RETRIEVAL_RESULT_LIMIT), list_document_nodes_by_lexical_search_from_db(document_ids, document_evidence_chunk_types, question, RETRIEVAL_RESULT_LIMIT)]

    if "go deeper" in question.casefold():
        previous_source_ids = list_previous_answer_source_ids_from_db(conversation_id)
        previous_sources = list_document_nodes_by_ids_db(document_ids, previous_source_ids)
        result_lists.insert(0, previous_sources)

    fused_nodes = reciprocal_rank_fusion(result_lists)
    diversified_nodes = diversify_ranked_nodes(fused_nodes, document_ids)

    if answer_depth == "overview":
        root_nodes = [node for node in diversified_nodes if node["node_type"] in {"root", "extralarge", "large", "overview", "major"}]
        document_evidence_chunks = [node for node in diversified_nodes if node["node_type"] in {"evidence", "table"}]
        ordered_nodes = root_nodes + document_evidence_chunks
    else:
        document_evidence_chunks = [node for node in diversified_nodes if node["node_type"] in {"evidence", "table"}]
        summary_nodes = [node for node in diversified_nodes if node["node_type"] not in {"evidence", "table"}]
        ordered_nodes = document_evidence_chunks + summary_nodes

    expanded_sources = expand_sources_with_neighboring_document_evidence_chunks(ordered_nodes)
    return limit_document_source_tokens(expanded_sources)
