import re


CITATION_PATTERN = re.compile(r"\[DOC:([0-9a-f-]{36}):PAGE:(\d+)\]", re.IGNORECASE)


def create_citation_label(node: dict) -> str:
    return f"[DOC:{node['document_id']}:PAGE:{node['page_start']}]"


def validate_and_link_citations(
    answer: str,
    documents: list[dict],
    evidence_nodes: list[dict],
    user_id: int,
) -> str:
    documents_by_id = {document["id"]: document for document in documents}

    def replace_citation(match: re.Match) -> str:
        document_id = match.group(1).lower()
        page_number = int(match.group(2))
        document = documents_by_id.get(document_id)

        if not document:
            return "[unavailable source]"

        maximum_page = int(document["page_count"] or 0)

        if page_number < 1 or page_number > maximum_page:
            return "[unavailable source]"

        filename = document["filename"].replace("[", "").replace("]", "")
        source_url = f"/api/documents/{document_id}/file?user_id={user_id}#page={page_number}"
        return f"[{filename}, p. {page_number}]({source_url})"

    validated_answer = CITATION_PATTERN.sub(replace_citation, answer)

    if CITATION_PATTERN.search(answer):
        return validated_answer

    source_lines: list[str] = []
    seen_sources: set[tuple[str, int]] = set()

    for node in evidence_nodes[:8]:
        source_key = (node["document_id"], node["page_start"])

        if source_key in seen_sources:
            continue

        seen_sources.add(source_key)
        document = documents_by_id[node["document_id"]]
        source_url = (
            f"/api/documents/{document['id']}/file"
            f"?user_id={user_id}#page={node['page_start']}"
        )
        source_lines.append(
            f"- [{document['filename']}, p. {node['page_start']}]({source_url})"
        )

    if not source_lines:
        return validated_answer

    return validated_answer.rstrip() + "\n\nSources:\n" + "\n".join(source_lines)
