import re
import numpy as np


from collections.abc import Callable
from dataclasses import dataclass
from ..core.config import DOCUMENT_CLUSTER_MAX_TOKENS, DOCUMENT_CLUSTER_SEED_TOKENS
from ..utils.chat_context import estimate_tokens
from .document_embedding_service import create_final_document_embeddings

EVIDENCE_MAX_TOKENS = 450
RECURSIVE_TEXT_SEPARATORS = (r"\n\s*\n", r"\n", r"(?<=[.!?])\s+", r"\s+")


@dataclass(frozen=True)
class DocumentEvidenceChunk:
    chunk_type: str
    title: str
    content: str
    page_start: int
    page_end: int
    token_count: int
    source_table_id: int | None = None


@dataclass(frozen=True)
class DocumentEvidenceChunkSeed:
    content: str
    page_number: int
    heading: str
    token_count: int


def split_text_into_document_evidence_chunks(text: str, maximum_tokens: int = DOCUMENT_CLUSTER_MAX_TOKENS, overlap_tokens: int = 0) -> list[str]:
    words = text.split()

    if not words:
        return []

    chunks = []
    chunk_start_index = 0

    while chunk_start_index < len(words):
        chunk_words = []
        next_word_index = chunk_start_index

        while next_word_index < len(words):
            candidate_text = " ".join(chunk_words + [words[next_word_index]])

            if chunk_words and estimate_tokens(candidate_text) > maximum_tokens:
                break

            chunk_words.append(words[next_word_index])
            next_word_index += 1

        chunks.append(" ".join(chunk_words))

        if next_word_index >= len(words):
            break

        overlap_start_index = find_overlap_start_index(words, chunk_start_index, next_word_index, overlap_tokens)
        chunk_start_index = overlap_start_index if overlap_start_index > chunk_start_index else next_word_index

    return chunks


def find_overlap_start_index(words: list[str], chunk_start_index: int, next_word_index: int, overlap_tokens: int) -> int:
    overlap_start_index = next_word_index

    while overlap_start_index > chunk_start_index:
        candidate_start_index = overlap_start_index - 1
        overlap_text = " ".join(words[candidate_start_index:next_word_index])

        if estimate_tokens(overlap_text) > overlap_tokens:
            break

        overlap_start_index = candidate_start_index

    return overlap_start_index


def split_text_recursively(text: str, maximum_tokens: int = DOCUMENT_CLUSTER_SEED_TOKENS, separator_index: int = 0) -> list[str]:
    normalized_text = text.strip()

    if not normalized_text:
        return []

    if estimate_tokens(normalized_text) <= maximum_tokens:
        return [normalized_text]

    if separator_index >= len(RECURSIVE_TEXT_SEPARATORS):
        return split_text_into_document_evidence_chunks(normalized_text, maximum_tokens)

    separator = RECURSIVE_TEXT_SEPARATORS[separator_index]
    pieces = [piece.strip() for piece in re.split(separator, normalized_text) if piece.strip()]

    if len(pieces) <= 1:
        return split_text_recursively(normalized_text, maximum_tokens, separator_index + 1)

    chunks = []
    current_piece_group = []

    for piece in pieces:
        candidate_text = " ".join(current_piece_group + [piece])

        if current_piece_group and estimate_tokens(candidate_text) > maximum_tokens:
            chunks.extend(split_text_recursively(" ".join(current_piece_group), maximum_tokens, separator_index + 1))
            current_piece_group = [piece]
        else:
            current_piece_group.append(piece)

    if current_piece_group:
        chunks.extend(split_text_recursively(" ".join(current_piece_group), maximum_tokens, separator_index + 1))

    return chunks


def create_document_evidence_chunk_seeds(pages: list[dict]) -> list[DocumentEvidenceChunkSeed]:
    seeds = []

    for page in pages:
        page_number = int(page["page_number"])
        page_heading = page["headings"][0] if page["headings"] else f"Page {page_number}"

        for content in split_text_recursively(page["narrative_text"]):
            seeds.append(DocumentEvidenceChunkSeed(content=content, page_number=page_number, heading=page_heading, token_count=estimate_tokens(content)))

    return seeds


def create_similarity_matrix(seed_embeddings: list[list[float]]) -> np.ndarray:
    embedding_matrix = np.asarray(seed_embeddings, dtype=np.float32)
    embedding_norms = np.linalg.norm(embedding_matrix, axis=1, keepdims=True)
    normalized_embeddings = embedding_matrix / np.maximum(embedding_norms, np.finfo(np.float32).eps)
    return normalized_embeddings @ normalized_embeddings.T


def calculate_similarity_baseline(similarity_matrix: np.ndarray) -> float:
    seed_count = len(similarity_matrix)

    if seed_count < 2:
        return 0.0

    return float(similarity_matrix[np.triu_indices(seed_count, k=1)].mean())


def calculate_cluster_reward(similarity_matrix: np.ndarray, start_index: int, end_index: int, similarity_baseline: float) -> float:
    cluster_size = end_index - start_index

    if cluster_size < 2:
        return 0.0

    cluster_similarities = similarity_matrix[start_index:end_index, start_index:end_index]
    pairwise_similarities = cluster_similarities[np.triu_indices(cluster_size, k=1)]
    return float((pairwise_similarities - similarity_baseline).sum())


def find_optimal_cluster_boundaries(seeds: list[DocumentEvidenceChunkSeed], similarity_matrix: np.ndarray, maximum_tokens: int = DOCUMENT_CLUSTER_MAX_TOKENS) -> list[tuple[int, int]]:
    seed_count = len(seeds)
    similarity_baseline = calculate_similarity_baseline(similarity_matrix)
    best_scores = [float("-inf")] * (seed_count + 1)
    previous_boundaries = [-1] * (seed_count + 1)
    best_scores[0] = 0.0

    for end_index in range(1, seed_count + 1):
        cluster_tokens = 0

        for start_index in range(end_index - 1, -1, -1):
            cluster_tokens += seeds[start_index].token_count

            if cluster_tokens > maximum_tokens and start_index < end_index - 1:
                break

            candidate_score = best_scores[start_index] + calculate_cluster_reward(similarity_matrix, start_index, end_index, similarity_baseline)

            if candidate_score > best_scores[end_index]:
                best_scores[end_index] = candidate_score
                previous_boundaries[end_index] = start_index

    boundaries = []
    end_index = seed_count

    while end_index > 0:
        start_index = previous_boundaries[end_index]

        if start_index < 0:
            raise RuntimeError("Could not create semantic cluster boundaries")

        boundaries.append((start_index, end_index))
        end_index = start_index

    return list(reversed(boundaries))


def create_cluster_title(cluster_seeds: list[DocumentEvidenceChunkSeed]) -> str:
    page_start = cluster_seeds[0].page_number
    page_end = cluster_seeds[-1].page_number
    heading = cluster_seeds[0].heading
    return f"{heading} - pages {page_start}-{page_end}"


def create_cluster_document_evidence_chunks(seeds: list[DocumentEvidenceChunkSeed], embedding_function: Callable[[list[str]], list[list[float]]]) -> list[DocumentEvidenceChunk]:
    if not seeds:
        return []

    seed_embeddings = embedding_function([seed.content for seed in seeds])

    if len(seed_embeddings) != len(seeds):
        raise RuntimeError("Semantic embedding count did not match the number of text seeds")

    similarity_matrix = create_similarity_matrix(seed_embeddings)
    boundaries = find_optimal_cluster_boundaries(seeds, similarity_matrix)
    document_evidence_chunks = []

    for start_index, end_index in boundaries:
        cluster_seeds = seeds[start_index:end_index]
        cluster_content = "\n\n".join(seed.content for seed in cluster_seeds)
        chunk = DocumentEvidenceChunk(chunk_type="evidence", title=create_cluster_title(cluster_seeds), content=cluster_content, page_start=cluster_seeds[0].page_number, page_end=cluster_seeds[-1].page_number, token_count=estimate_tokens(cluster_content))
        document_evidence_chunks.append(chunk)

    return document_evidence_chunks


def split_markdown_table_into_chunks(markdown: str) -> list[str]:
    table_lines = [line for line in markdown.splitlines() if line.strip()]

    if len(table_lines) <= 2 or estimate_tokens(markdown) <= EVIDENCE_MAX_TOKENS:
        return [markdown] if markdown else []

    header_lines = table_lines[:2]
    table_chunks = []
    current_data_lines = []

    for data_line in table_lines[2:]:
        candidate_markdown = "\n".join(header_lines + current_data_lines + [data_line])

        if current_data_lines and estimate_tokens(candidate_markdown) > EVIDENCE_MAX_TOKENS:
            table_chunks.append("\n".join(header_lines + current_data_lines))
            current_data_lines = []

        current_data_lines.append(data_line)

    if current_data_lines:
        table_chunks.append("\n".join(header_lines + current_data_lines))

    return table_chunks


def create_table_document_evidence_chunks(page: dict) -> list[DocumentEvidenceChunk]:
    page_number = int(page["page_number"])
    document_evidence_chunks = []

    for table in page["tables"]:
        table_title = f"Page {page_number}, table {table['table_number']}"
        markdown_chunks = split_markdown_table_into_chunks(table["markdown"])

        for chunk_number, content in enumerate(markdown_chunks, start=1):
            chunk_title = f"{table_title}, part {chunk_number}" if len(markdown_chunks) > 1 else table_title
            chunk = DocumentEvidenceChunk(chunk_type="table", title=chunk_title, content=content, page_start=page_number, page_end=page_number, token_count=estimate_tokens(content), source_table_id=table["id"])
            document_evidence_chunks.append(chunk)

    return document_evidence_chunks


def create_document_evidence_chunks(pages: list[dict], embedding_function: Callable[[list[str]], list[list[float]]] | None = None) -> list[DocumentEvidenceChunk]:
    embedding_function = create_final_document_embeddings if embedding_function is None else embedding_function

    document_evidence_chunk_seeds = create_document_evidence_chunk_seeds(pages)
    narrative_chunks = create_cluster_document_evidence_chunks(document_evidence_chunk_seeds, embedding_function)
    table_chunks = [chunk for page in pages for chunk in create_table_document_evidence_chunks(page)]
    return narrative_chunks + table_chunks
