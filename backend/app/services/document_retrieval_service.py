from collections import defaultdict

from ..core.config import MAX_DOCUMENT_EVIDENCE_TOKENS
from ..infrastructure.document_repository import (
    list_all_document_leaf_nodes_from_db,
    list_document_nodes_by_ids_from_db,
    list_document_nodes_by_lexical_search_from_db,
    list_document_nodes_by_vector_from_db,
    list_neighboring_document_nodes_from_db,
    list_previous_answer_evidence_ids_from_db,
)
from .document_embedding_service import create_document_query_embedding


RETRIEVAL_RESULT_LIMIT = 30
RECIPROCAL_RANK_CONSTANT = 60


def classify_answer_depth(question: str) -> str:
    normalized_question = question.casefold()
    evidence_phrases = (
        "exact value",
        "exact number",
        "according to the table",
        "which table",
        "what unit",
        "page-level",
    )
    overview_phrases = (
        "briefly",
        "overall",
        "overview",
        "summarize the document",
        "main conclusion",
    )
    deeper_phrases = ("go deeper", "more detail", "in depth", "elaborate")

    if any(phrase in normalized_question for phrase in evidence_phrases):
        return "evidence"

    if any(phrase in normalized_question for phrase in deeper_phrases):
        return "evidence"

    if any(phrase in normalized_question for phrase in overview_phrases):
        return "overview"

    if len(question.split()) <= 8:
        return "focused"

    return "section"


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
        first_document_node = next(
            (node for node in nodes if node["document_id"] == document_id),
            None,
        )

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


def expand_with_neighboring_evidence(nodes: list[dict], maximum_seed_count: int = 8) -> list[dict]:
    expanded_nodes = list(nodes)
    included_node_ids = {node["id"] for node in nodes}

    for seed_node in nodes[:maximum_seed_count]:
        neighboring_nodes = list_neighboring_document_nodes_from_db(
            seed_node["document_id"],
            seed_node["page_start"],
            seed_node["page_end"],
        )

        for neighboring_node in neighboring_nodes:
            if neighboring_node["id"] in included_node_ids:
                continue

            expanded_nodes.append(neighboring_node)
            included_node_ids.add(neighboring_node["id"])

    return expanded_nodes


def apply_evidence_token_budget(nodes: list[dict]) -> list[dict]:
    selected_nodes: list[dict] = []
    used_tokens = 0

    for node in nodes:
        projected_tokens = used_tokens + int(node["token_count"]) + 30

        if projected_tokens > MAX_DOCUMENT_EVIDENCE_TOKENS:
            continue

        selected_nodes.append(node)
        used_tokens = projected_tokens

    return selected_nodes


def retrieve_document_evidence(
    question: str,
    documents: list[dict],
    conversation_id: int,
    answer_depth: str,
) -> list[dict]:
    if len(documents) == 1 and documents[0]["analysis_mode"] == "basic":
        return list_all_document_leaf_nodes_from_db(documents[0]["id"])

    document_ids = [document["id"] for document in documents]
    query_embedding = create_document_query_embedding(question)
    summary_types = ["root", "major", "section", "packet"]
    evidence_types = ["evidence", "table"]
    result_lists = [
        list_document_nodes_by_vector_from_db(
            document_ids,
            summary_types,
            query_embedding,
            RETRIEVAL_RESULT_LIMIT,
        ),
        list_document_nodes_by_lexical_search_from_db(
            document_ids,
            summary_types,
            question,
            RETRIEVAL_RESULT_LIMIT,
        ),
        list_document_nodes_by_vector_from_db(
            document_ids,
            evidence_types,
            query_embedding,
            RETRIEVAL_RESULT_LIMIT,
        ),
        list_document_nodes_by_lexical_search_from_db(
            document_ids,
            evidence_types,
            question,
            RETRIEVAL_RESULT_LIMIT,
        ),
    ]

    if "go deeper" in question.casefold():
        previous_node_ids = list_previous_answer_evidence_ids_from_db(conversation_id)
        previous_nodes = list_document_nodes_by_ids_from_db(document_ids, previous_node_ids)
        result_lists.insert(0, previous_nodes)

    fused_nodes = reciprocal_rank_fusion(result_lists)
    diversified_nodes = diversify_ranked_nodes(fused_nodes, document_ids)

    if answer_depth == "overview":
        root_nodes = [
            node
            for node in diversified_nodes
            if node["node_type"] in {"root", "major"}
        ]
        leaf_nodes = [
            node
            for node in diversified_nodes
            if node["node_type"] in {"evidence", "table"}
        ]
        ordered_nodes = root_nodes + leaf_nodes
    else:
        leaf_nodes = [
            node
            for node in diversified_nodes
            if node["node_type"] in {"evidence", "table"}
        ]
        summary_nodes = [
            node
            for node in diversified_nodes
            if node["node_type"] not in {"evidence", "table"}
        ]
        ordered_nodes = leaf_nodes + summary_nodes

    expanded_nodes = expand_with_neighboring_evidence(ordered_nodes)
    return apply_evidence_token_budget(expanded_nodes)
