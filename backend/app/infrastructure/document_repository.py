from typing import Iterable

from psycopg.types.json import Jsonb

from .database import open_database_connection

DOCUMENT_COLUMNS = """
id, user_id, filename, mime_type, storage_name, checksum_sha256,
analysis_version, status, analysis_mode, progress_percent, page_count,
extracted_token_count, summary, last_error, created_at, updated_at
"""


def qualify_document_columns(table_name: str) -> str:
    column_names = [column.strip() for column in DOCUMENT_COLUMNS.split(",")]
    qualified_names = [f"{table_name}.{column_name}" for column_name in column_names]
    return ", ".join(qualified_names)


def create_or_get_document_from_db(document_id: str, user_id: int, filename: str, storage_name: str, checksum_sha256: str, analysis_version: str) -> tuple[dict, bool]:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            document = database_connection.execute(
                f"""
                INSERT INTO documents (id, user_id, filename, storage_name, checksum_sha256, analysis_version )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, checksum_sha256, analysis_version) DO NOTHING
                RETURNING {DOCUMENT_COLUMNS}
                """,
                (document_id, user_id, filename, storage_name, checksum_sha256, analysis_version),
            ).fetchone()

            if not document:
                existing_document = database_connection.execute(f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE user_id = %s AND checksum_sha256 = %s AND analysis_version = %s", (user_id, checksum_sha256, analysis_version)).fetchone()

                if not existing_document:
                    raise RuntimeError("Conflicting document record could not be loaded")

                return existing_document, False

            database_connection.execute("INSERT INTO document_analysis_jobs (document_id) VALUES (%s)", (document_id,))
            return document, True


def get_document_for_user_from_db(document_id: str, user_id: int) -> dict | None:
    with open_database_connection() as database_connection:
        return database_connection.execute(f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE id = %s AND user_id = %s", (document_id, user_id)).fetchone()


def get_document_by_id_from_db(document_id: str) -> dict | None:
    with open_database_connection() as database_connection:
        return database_connection.execute(f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE id = %s", (document_id,)).fetchone()


def list_documents_for_user_from_db(user_id: int) -> list[dict]:
    with open_database_connection() as database_connection:
        return database_connection.execute(f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE user_id = %s ORDER BY updated_at DESC, created_at DESC", (user_id,)).fetchall()


def attach_document_to_conversation_from_db(conversation_id: int, document_id: str, user_id: int) -> bool:
    with open_database_connection() as database_connection:
        document = database_connection.execute(
            """
            SELECT documents.id
            FROM documents
            JOIN conversations ON conversations.id = %s
            WHERE documents.id = %s
              AND documents.user_id = %s
              AND conversations.user_id = %s
            """,
            (conversation_id, document_id, user_id, user_id),
        ).fetchone()

        if not document:
            return False

        database_connection.execute("INSERT INTO conversation_documents (conversation_id, document_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (conversation_id, document_id))
        return True


def detach_document_from_conversation_from_db(conversation_id: int, document_id: str, user_id: int) -> bool:
    with open_database_connection() as database_connection:
        result = database_connection.execute(
            """
            DELETE FROM conversation_documents
            USING conversations, documents
            WHERE conversation_documents.conversation_id = conversations.id
              AND conversation_documents.document_id = documents.id
              AND conversations.id = %s
              AND documents.id = %s
              AND conversations.user_id = %s
              AND documents.user_id = %s
            """,
            (conversation_id, document_id, user_id, user_id),
        )
        return result.rowcount > 0


def list_conversation_documents_from_db(conversation_id: int, user_id: int) -> list[dict]:
    qualified_columns = qualify_document_columns("documents")

    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {qualified_columns}
            FROM conversation_documents
            JOIN documents ON documents.id = conversation_documents.document_id
            JOIN conversations ON conversations.id = conversation_documents.conversation_id
            WHERE conversation_documents.conversation_id = %s
              AND conversations.user_id = %s
              AND documents.user_id = %s
            ORDER BY conversation_documents.attached_at, documents.id
            """,
            (conversation_id, user_id, user_id),
        ).fetchall()


def link_message_to_documents_db(message_id: int, document_ids: Iterable[str]) -> None:
    with open_database_connection() as database_connection:
        for document_id in document_ids:
            database_connection.execute("INSERT INTO message_documents (message_id, document_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (message_id, document_id))


def insert_doc_pages_and_tables_db(document_id: str, extracted_document) -> None:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            database_connection.execute("DELETE FROM document_nodes WHERE document_id = %s", (document_id,))
            database_connection.execute("DELETE FROM document_pages WHERE document_id = %s", (document_id,))
            database_connection.execute("DELETE FROM document_tables WHERE document_id = %s", (document_id,))

            for page in extracted_document.pages:
                database_connection.execute(
                    """
                    INSERT INTO document_pages (
                        document_id, page_number, narrative_text, token_count,
                        headings, extraction_warnings
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (document_id, page.page_number, page.narrative_text, page.token_count, Jsonb(page.headings), Jsonb(page.extraction_warnings)),
                )

                for table in page.tables:
                    database_connection.execute(
                        """
                        INSERT INTO document_tables (
                            document_id, page_number, table_number, rows, markdown, token_count
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (document_id, page.page_number, table.table_number, Jsonb(table.rows), table.markdown, table.token_count),
                    )

            database_connection.execute(
                """
                UPDATE documents
                SET page_count = %s, extracted_token_count = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (len(extracted_document.pages), extracted_document.token_count, document_id),
            )


def combine_document_pages_with_tables(document_id: str) -> list[dict]:
    with open_database_connection() as database_connection:
        pages = database_connection.execute(
            """
            SELECT id, document_id, page_number, narrative_text, token_count,
                   headings, extraction_warnings
            FROM document_pages
            WHERE document_id = %s
            ORDER BY page_number
            """,
            (document_id,),
        ).fetchall()
        tables = database_connection.execute(
            """
            SELECT id, document_id, page_number, table_number, rows, markdown, token_count
            FROM document_tables
            WHERE document_id = %s
            ORDER BY page_number, table_number
            """,
            (document_id,),
        ).fetchall()

    tables_by_page: dict[int, list[dict]] = {}

    for table in tables:
        page_tables = tables_by_page.setdefault(table["page_number"], [])
        page_tables.append(table)

    for page in pages:
        page["tables"] = tables_by_page.get(page["page_number"], [])

    return pages


def update_document_status_db(document_id: str, status: str, progress_percent: int, *, analysis_mode: str | None = None, summary: str | None = None, last_error: str | None = None) -> None:
    with open_database_connection() as database_connection:
        database_connection.execute(
            """
            UPDATE documents
            SET status = %s,
                progress_percent = %s,
                analysis_mode = COALESCE(%s, analysis_mode),
                summary = COALESCE(%s, summary),
                last_error = COALESCE(%s, last_error),
                updated_at = NOW()
            WHERE id = %s
            """,
            (status, progress_percent, analysis_mode, summary, last_error, document_id),
        )


def delete_document_for_user_from_db(document_id: str, user_id: int) -> dict | None:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            document = database_connection.execute("SELECT id, storage_name FROM documents WHERE id = %s AND user_id = %s FOR UPDATE", (document_id, user_id)).fetchone()

            if not document:
                return None

            citation_pattern = r"\[[^]]+\]\(/api/documents/" + document_id + r"/file\?user_id=[0-9]+#page=[0-9]+\)"
            database_connection.execute(
                """
                UPDATE messages
                SET content = regexp_replace(content, %s, '[unavailable source]', 'g')
                WHERE role = 'assistant'
                  AND id IN (
                      SELECT message_id
                      FROM message_documents
                      WHERE document_id = %s
                  )
                """,
                (citation_pattern, document_id),
            )
            return database_connection.execute("DELETE FROM documents WHERE id = %s RETURNING storage_name", (document_id,)).fetchone()
