from dataclasses import dataclass

from ..utils.chat_context import estimate_tokens


EVIDENCE_TARGET_TOKENS = 400
EVIDENCE_MAX_TOKENS = 450
EVIDENCE_OVERLAP_TOKENS = 60


@dataclass(frozen=True)
class EvidenceChunk:
    node_type: str
    title: str
    content: str
    page_start: int
    page_end: int
    token_count: int


def split_text_into_word_chunks(
    text: str,
    maximum_tokens: int = EVIDENCE_MAX_TOKENS,
    overlap_tokens: int = EVIDENCE_OVERLAP_TOKENS,
) -> list[str]:
    words = text.split()

    if not words:
        return []

    chunks: list[str] = []
    chunk_start = 0

    while chunk_start < len(words):
        chunk_words: list[str] = []
        word_index = chunk_start

        while word_index < len(words):
            candidate_words = chunk_words + [words[word_index]]
            candidate_text = " ".join(candidate_words)

            if chunk_words and estimate_tokens(candidate_text) > maximum_tokens:
                break

            chunk_words.append(words[word_index])
            word_index += 1

        chunk_text = " ".join(chunk_words).strip()

        if chunk_text:
            chunks.append(chunk_text)

        if word_index >= len(words):
            break

        overlap_start = word_index
        overlap_text = ""

        while overlap_start > chunk_start:
            candidate_overlap_start = overlap_start - 1
            candidate_overlap = " ".join(words[candidate_overlap_start:word_index])

            if estimate_tokens(candidate_overlap) > overlap_tokens:
                break

            overlap_start = candidate_overlap_start
            overlap_text = candidate_overlap

        next_chunk_start = overlap_start if overlap_text else word_index

        if next_chunk_start <= chunk_start:
            next_chunk_start = word_index

        chunk_start = next_chunk_start

    return chunks


def create_narrative_evidence_chunks(page: dict) -> list[EvidenceChunk]:
    page_number = int(page["page_number"])
    heading = page["headings"][0] if page["headings"] else f"Page {page_number}"
    narrative_chunks = split_text_into_word_chunks(page["narrative_text"])
    evidence_chunks: list[EvidenceChunk] = []

    for chunk_number, content in enumerate(narrative_chunks, start=1):
        title = f"{heading} — passage {chunk_number}"
        evidence_chunks.append(
            EvidenceChunk(
                node_type="evidence",
                title=title,
                content=content,
                page_start=page_number,
                page_end=page_number,
                token_count=estimate_tokens(content),
            )
        )

    return evidence_chunks


def create_table_evidence_chunks(page: dict) -> list[EvidenceChunk]:
    page_number = int(page["page_number"])
    evidence_chunks: list[EvidenceChunk] = []

    for table in page["tables"]:
        title = f"Page {page_number}, table {table['table_number']}"
        table_chunks = split_markdown_table_into_chunks(table["markdown"])

        for chunk_number, content in enumerate(table_chunks, start=1):
            chunk_title = title

            if len(table_chunks) > 1:
                chunk_title = f"{title}, part {chunk_number}"

            evidence_chunks.append(
                EvidenceChunk(
                    node_type="table",
                    title=chunk_title,
                    content=content,
                    page_start=page_number,
                    page_end=page_number,
                    token_count=estimate_tokens(content),
                )
            )

    return evidence_chunks


def split_markdown_table_into_chunks(markdown: str) -> list[str]:
    table_lines = [line for line in markdown.splitlines() if line.strip()]

    if len(table_lines) <= 2 or estimate_tokens(markdown) <= EVIDENCE_MAX_TOKENS:
        return [markdown] if markdown else []

    header_lines = table_lines[:2]
    data_lines = table_lines[2:]
    table_chunks: list[str] = []
    current_data_lines: list[str] = []

    for data_line in data_lines:
        candidate_lines = header_lines + current_data_lines + [data_line]
        candidate_markdown = "\n".join(candidate_lines)

        if current_data_lines and estimate_tokens(candidate_markdown) > EVIDENCE_MAX_TOKENS:
            table_chunks.append("\n".join(header_lines + current_data_lines))
            current_data_lines = []

        current_data_lines.append(data_line)

    if current_data_lines:
        table_chunks.append("\n".join(header_lines + current_data_lines))

    return table_chunks


def create_document_evidence_chunks(pages: list[dict]) -> list[EvidenceChunk]:
    evidence_chunks: list[EvidenceChunk] = []

    for page in pages:
        narrative_chunks = create_narrative_evidence_chunks(page)
        table_chunks = create_table_evidence_chunks(page)
        evidence_chunks.extend(narrative_chunks)
        evidence_chunks.extend(table_chunks)

    return evidence_chunks
