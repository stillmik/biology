import re
from functools import partial

CITATION_PATTERN = re.compile(r"\[DOC:([0-9a-f-]{36}):PAGE:(\d+)\]", re.IGNORECASE)


def create_citation_label(source: dict) -> str:
    page_start = int(source["page_start"])
    page_end = int(source.get("page_end", page_start))
    return " ".join(f"[DOC:{source['document_id']}:PAGE:{page_number}]" for page_number in range(page_start, page_end + 1))


def replace_citation_match(match: re.Match, documents_by_id: dict[str, dict], allowed_citation_pages: set[tuple[str, int]], user_id: int) -> str:
    document_id = match.group(1).lower()
    page_number = int(match.group(2))
    document = documents_by_id.get(document_id)

    if not document:
        return "[unavailable source]"

    if (document_id, page_number) not in allowed_citation_pages:
        return "[unavailable source]"

    maximum_page = int(document["page_count"] or 0)

    if page_number < 1 or page_number > maximum_page:
        return "[unavailable source]"

    filename = document["filename"].replace("[", "").replace("]", "")
    source_url = f"/api/documents/{document_id}/file?user_id={user_id}#page={page_number}"
    return f"[{filename}, p. {page_number}]({source_url})"


def validate_and_link_citations(answer: str, documents: list[dict], retrieved_sources: list[dict], user_id: int) -> str:
    documents_by_id = {str(document["id"]).lower(): document for document in documents}
    allowed_citation_pages = {(source["document_id"].lower(), page_number) for source in retrieved_sources for page_number in range(int(source["page_start"]), int(source.get("page_end", source["page_start"])) + 1)}
    citation_replacer = partial(replace_citation_match, documents_by_id=documents_by_id, allowed_citation_pages=allowed_citation_pages, user_id=user_id)
    validated_answer = CITATION_PATTERN.sub(citation_replacer, answer)

    if CITATION_PATTERN.search(answer):
        return validated_answer

    source_lines: list[str] = []
    seen_sources: set[tuple[str, int]] = set()

    for source in retrieved_sources[:8]:
        source_key = (source["document_id"], source["page_start"])

        if source_key in seen_sources:
            continue

        seen_sources.add(source_key)
        document = documents_by_id[source["document_id"]]
        source_url = f"/api/documents/{document['id']}/file?user_id={user_id}#page={source['page_start']}"
        source_line = f"- [{document['filename']}, p. {source['page_start']}]({source_url})"
        source_lines.append(source_line)

    if not source_lines:
        return validated_answer

    return validated_answer.rstrip() + "\n\nSources:\n" + "\n".join(source_lines)
