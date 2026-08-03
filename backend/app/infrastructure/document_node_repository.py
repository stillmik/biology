from .database import open_database_connection

DOCUMENT_NODE_COLUMNS = """
document_nodes.id, document_nodes.document_id, document_nodes.parent_id,
document_nodes.node_type, document_nodes.hierarchy_level,
document_nodes.source_table_id, document_nodes.title, document_nodes.content,
document_nodes.page_start, document_nodes.page_end, document_nodes.token_count
"""
DOCUMENT_NODE_RETURN_COLUMNS = """
id, document_id, parent_id, node_type, hierarchy_level, source_table_id,
title, content, page_start, page_end, token_count
"""


def vector_to_postgres_literal(embedding: list[float]) -> str:
    serialized_values = ",".join(str(value) for value in embedding)
    return "[" + serialized_values + "]"


def delete_document_nodes_from_db(document_id: str) -> None:
    with open_database_connection() as database_connection:
        database_connection.execute("DELETE FROM document_nodes WHERE document_id = %s", (document_id,))


def create_document_node_db(document_id: str, node_type: str, hierarchy_level: int, title: str, content: str, page_start: int, page_end: int, token_count: int, embedding: list[float], parent_id: int | None = None, source_table_id: int | None = None) -> dict:
    vector_literal = vector_to_postgres_literal(embedding)

    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            INSERT INTO document_nodes (
                document_id, parent_id, node_type, hierarchy_level,
                source_table_id, title, content, page_start, page_end,
                token_count, embedding
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
            RETURNING {DOCUMENT_NODE_RETURN_COLUMNS}
            """,
            (document_id, parent_id, node_type, hierarchy_level, source_table_id, title, content, page_start, page_end, token_count, vector_literal),
        ).fetchone()


def create_document_evidence_chunk_db(document_id: str, chunk_type: str, title: str, content: str, page_start: int, page_end: int, token_count: int, embedding: list[float], source_table_id: int | None = None) -> dict:
    return create_document_node_db(document_id, chunk_type, 0, title, content, page_start, page_end, token_count, embedding, source_table_id=source_table_id)


def get_existing_summary_node_from_db(document_id: str, node_type: str, page_start: int, page_end: int) -> dict | None:
    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {DOCUMENT_NODE_COLUMNS}
            FROM document_nodes
            WHERE document_id = %s
              AND node_type = %s
              AND page_start = %s
              AND page_end = %s
            LIMIT 1
            """,
            (document_id, node_type, page_start, page_end),
        ).fetchone()


def set_document_node_parent_from_db(node_ids: list[int], parent_id: int) -> None:
    if not node_ids:
        return

    with open_database_connection() as database_connection:
        database_connection.execute("UPDATE document_nodes SET parent_id = %s WHERE id = ANY(%s)", (parent_id, node_ids))


def create_document_node_sources_from_db(summary_node_id: int, source_node_ids: list[int]) -> None:
    with open_database_connection() as database_connection:
        for source_node_id in source_node_ids:
            database_connection.execute(
                """
                INSERT INTO document_node_sources (summary_node_id, source_node_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (summary_node_id, source_node_id),
            )


def list_document_nodes_by_ids_db(document_ids: list[str], node_ids: list[int]) -> list[dict]:
    if not node_ids:
        return []

    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {DOCUMENT_NODE_COLUMNS}, documents.filename
            FROM document_nodes
            JOIN documents ON documents.id = document_nodes.document_id
            WHERE document_nodes.document_id = ANY(%s)
              AND document_nodes.id = ANY(%s)
            ORDER BY array_position(%s::bigint[], document_nodes.id)
            """,
            (document_ids, node_ids, node_ids),
        ).fetchall()


def list_document_evidence_chunks_by_ids_db(document_ids: list[str], document_evidence_chunk_ids: list[int]) -> list[dict]:
    if not document_evidence_chunk_ids:
        return []

    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {DOCUMENT_NODE_COLUMNS}, documents.filename
            FROM document_nodes
            JOIN documents ON documents.id = document_nodes.document_id
            WHERE document_nodes.document_id = ANY(%s)
              AND document_nodes.id = ANY(%s)
              AND document_nodes.node_type IN ('evidence', 'table')
            ORDER BY array_position(%s::bigint[], document_nodes.id)
            """,
            (document_ids, document_evidence_chunk_ids, document_evidence_chunk_ids),
        ).fetchall()


def list_document_evidence_chunks_from_db(document_id: str) -> list[dict]:
    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {DOCUMENT_NODE_COLUMNS}, documents.filename
            FROM document_nodes
            JOIN documents ON documents.id = document_nodes.document_id
            WHERE document_nodes.document_id = %s
              AND document_nodes.node_type IN ('evidence', 'table')
            ORDER BY document_nodes.page_start, document_nodes.id
            """,
            (document_id,),
        ).fetchall()


def list_document_evidence_chunk_ids_db(document_id: str) -> list[int]:
    with open_database_connection() as database_connection:
        rows = database_connection.execute(
            """
            SELECT id
            FROM document_nodes
            WHERE document_id = %s AND node_type IN ('evidence', 'table')
            ORDER BY id
            """,
            (document_id,),
        ).fetchall()
        return [row["id"] for row in rows]


def document_has_root_summary_node_db(document_id: str) -> bool:
    with open_database_connection() as database_connection:
        row = database_connection.execute("SELECT EXISTS (SELECT 1 FROM document_nodes WHERE document_id = %s AND node_type = 'root') AS exists", (document_id,)).fetchone()
        return bool(row["exists"])


def count_uncovered_nonblank_document_pages_from_db(document_id: str) -> int:
    with open_database_connection() as database_connection:
        row = database_connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM document_pages
            WHERE document_id = %s
              AND (narrative_text <> '' OR EXISTS (
                  SELECT 1
                  FROM document_tables
                  WHERE document_tables.document_id = document_pages.document_id
                    AND document_tables.page_number = document_pages.page_number
              ))
              AND NOT EXISTS (
                  SELECT 1
                  FROM document_nodes
                  WHERE document_nodes.document_id = document_pages.document_id
                    AND document_nodes.node_type IN ('evidence', 'table')
                    AND document_nodes.page_start <= document_pages.page_number
                    AND document_nodes.page_end >= document_pages.page_number
              )
            """,
            (document_id,),
        ).fetchone()
        return int(row["count"])


def count_uncovered_document_tables_from_db(document_id: str) -> int:
    with open_database_connection() as database_connection:
        row = database_connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM document_tables
            WHERE document_id = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM document_nodes
                  WHERE document_nodes.source_table_id = document_tables.id
                    AND document_nodes.node_type = 'table'
              )
            """,
            (document_id,),
        ).fetchone()
        return int(row["count"])


def count_unprovenanced_summary_nodes_from_db(document_id: str) -> int:
    with open_database_connection() as database_connection:
        row = database_connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM document_nodes
            WHERE document_id = %s
              AND node_type IN ('packet', 'section', 'major', 'overview', 'small', 'medium', 'large', 'extralarge', 'root')
              AND NOT EXISTS (
                  SELECT 1
                  FROM document_node_sources
                  WHERE document_node_sources.summary_node_id = document_nodes.id
              )
            """,
            (document_id,),
        ).fetchone()
        return int(row["count"])


def list_document_nodes_by_vector_from_db(document_ids: list[str], node_types: list[str], query_embedding: list[float], limit: int) -> list[dict]:
    vector_literal = vector_to_postgres_literal(query_embedding)

    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {DOCUMENT_NODE_COLUMNS}, documents.filename,
                   1 - (document_nodes.embedding <=> %s::vector) AS score
            FROM document_nodes
            JOIN documents ON documents.id = document_nodes.document_id
            WHERE document_nodes.document_id = ANY(%s)
              AND document_nodes.node_type = ANY(%s)
              AND document_nodes.embedding IS NOT NULL
            ORDER BY document_nodes.embedding <=> %s::vector
            LIMIT %s
            """,
            (vector_literal, document_ids, node_types, vector_literal, limit),
        ).fetchall()


def list_document_nodes_by_lexical_search_from_db(document_ids: list[str], node_types: list[str], question: str, limit: int) -> list[dict]:
    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {DOCUMENT_NODE_COLUMNS}, documents.filename,
                   ts_rank_cd(document_nodes.search_vector, websearch_to_tsquery('english', %s)) AS score
            FROM document_nodes
            JOIN documents ON documents.id = document_nodes.document_id
            WHERE document_nodes.document_id = ANY(%s)
              AND document_nodes.node_type = ANY(%s)
              AND document_nodes.search_vector @@ websearch_to_tsquery('english', %s)
            ORDER BY score DESC
            LIMIT %s
            """,
            (question, document_ids, node_types, question, limit),
        ).fetchall()


def list_neighboring_document_evidence_chunks_from_db(document_id: str, page_start: int, page_end: int) -> list[dict]:
    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {DOCUMENT_NODE_COLUMNS}, documents.filename
            FROM document_nodes
            JOIN documents ON documents.id = document_nodes.document_id
            WHERE document_nodes.document_id = %s
              AND document_nodes.node_type IN ('evidence', 'table')
              AND document_nodes.page_start <= %s
              AND document_nodes.page_end >= %s
            ORDER BY document_nodes.page_start, document_nodes.id
            """,
            (document_id, page_end + 1, max(1, page_start - 1)),
        ).fetchall()


def save_answer_sources_db(assistant_message_id: int, source_ids: list[int]) -> None:
    with open_database_connection() as database_connection:
        for rank, source_id in enumerate(source_ids, start=1):
            database_connection.execute(
                """
                INSERT INTO answer_evidence (assistant_message_id, source_id, rank)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (assistant_message_id, source_id, rank),
            )


def list_previous_answer_source_ids_from_db(conversation_id: int) -> list[int]:
    with open_database_connection() as database_connection:
        rows = database_connection.execute(
            """
            SELECT answer_evidence.source_id
            FROM answer_evidence
            JOIN messages ON messages.id = answer_evidence.assistant_message_id
            WHERE messages.conversation_id = %s
            ORDER BY messages.id DESC, answer_evidence.rank
            LIMIT 20
            """,
            (conversation_id,),
        ).fetchall()
        return [row["source_id"] for row in rows]
