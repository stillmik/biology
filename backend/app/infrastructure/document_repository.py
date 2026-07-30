from typing import Iterable

from psycopg.types.json import Jsonb

from .database import open_database_connection_from_db


DOCUMENT_COLUMNS = """
id, user_id, filename, mime_type, storage_name, checksum_sha256,
analysis_version, status, analysis_mode, progress_percent, page_count,
extracted_token_count, summary, last_error, created_at, updated_at
"""


def create_or_get_document_from_db(
    document_id: str,
    user_id: int,
    filename: str,
    storage_name: str,
    checksum_sha256: str,
    analysis_version: str,
) -> tuple[dict, bool]:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            existing_document = database_connection.execute(
                f"""
                SELECT {DOCUMENT_COLUMNS}
                FROM documents
                WHERE user_id = %s AND checksum_sha256 = %s AND analysis_version = %s
                FOR UPDATE
                """,
                (user_id, checksum_sha256, analysis_version),
            ).fetchone()

            if existing_document:
                return existing_document, False

            document = database_connection.execute(
                f"""
                INSERT INTO documents (
                    id, user_id, filename, storage_name, checksum_sha256, analysis_version
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {DOCUMENT_COLUMNS}
                """,
                (document_id, user_id, filename, storage_name, checksum_sha256, analysis_version),
            ).fetchone()
            database_connection.execute(
                "INSERT INTO document_analysis_jobs (document_id) VALUES (%s)",
                (document_id,),
            )
            return document, True


def get_document_for_user_from_db(document_id: str, user_id: int) -> dict | None:
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE id = %s AND user_id = %s",
            (document_id, user_id),
        ).fetchone()


def get_document_by_id_from_db(document_id: str) -> dict | None:
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()


def list_documents_for_user_from_db(user_id: int) -> list[dict]:
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {DOCUMENT_COLUMNS}
            FROM documents
            WHERE user_id = %s
            ORDER BY updated_at DESC, created_at DESC
            """,
            (user_id,),
        ).fetchall()


def attach_document_to_conversation_from_db(
    conversation_id: int,
    document_id: str,
    user_id: int,
) -> bool:
    with open_database_connection_from_db() as database_connection:
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

        database_connection.execute(
            """
            INSERT INTO conversation_documents (conversation_id, document_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (conversation_id, document_id),
        )
        return True


def detach_document_from_conversation_from_db(
    conversation_id: int,
    document_id: str,
    user_id: int,
) -> bool:
    with open_database_connection_from_db() as database_connection:
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
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {", ".join(f"documents.{column.strip()}" for column in DOCUMENT_COLUMNS.split(","))}
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


def link_message_to_documents_from_db(message_id: int, document_ids: Iterable[str]) -> None:
    with open_database_connection_from_db() as database_connection:
        for document_id in document_ids:
            database_connection.execute(
                """
                INSERT INTO message_documents (message_id, document_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (message_id, document_id),
            )


def replace_document_extraction_from_db(document_id: str, extracted_document) -> None:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            database_connection.execute(
                "DELETE FROM document_pages WHERE document_id = %s",
                (document_id,),
            )
            database_connection.execute(
                "DELETE FROM document_tables WHERE document_id = %s",
                (document_id,),
            )

            for page in extracted_document.pages:
                database_connection.execute(
                    """
                    INSERT INTO document_pages (
                        document_id, page_number, narrative_text, token_count,
                        headings, extraction_warnings
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        document_id,
                        page.page_number,
                        page.narrative_text,
                        page.token_count,
                        Jsonb(page.headings),
                        Jsonb(page.extraction_warnings),
                    ),
                )

                for table in page.tables:
                    database_connection.execute(
                        """
                        INSERT INTO document_tables (
                            document_id, page_number, table_number, rows, markdown, token_count
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            document_id,
                            page.page_number,
                            table.table_number,
                            Jsonb(table.rows),
                            table.markdown,
                            table.token_count,
                        ),
                    )

            database_connection.execute(
                """
                UPDATE documents
                SET page_count = %s,
                    extracted_token_count = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (len(extracted_document.pages), extracted_document.token_count, document_id),
            )


def list_document_pages_with_tables_from_db(document_id: str) -> list[dict]:
    with open_database_connection_from_db() as database_connection:
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


def replace_document_nodes_from_db(document_id: str) -> None:
    with open_database_connection_from_db() as database_connection:
        database_connection.execute(
            "DELETE FROM document_nodes WHERE document_id = %s",
            (document_id,),
        )


def create_document_node_from_db(
    document_id: str,
    node_type: str,
    hierarchy_level: int,
    title: str,
    content: str,
    page_start: int,
    page_end: int,
    token_count: int,
    embedding: list[float],
    parent_id: int | None = None,
) -> dict:
    vector_literal = "[" + ",".join(str(value) for value in embedding) + "]"

    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            """
            INSERT INTO document_nodes (
                document_id, parent_id, node_type, hierarchy_level, title,
                content, page_start, page_end, token_count, embedding
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
            RETURNING id, document_id, parent_id, node_type, hierarchy_level,
                      title, content, page_start, page_end, token_count
            """,
            (
                document_id,
                parent_id,
                node_type,
                hierarchy_level,
                title,
                content,
                page_start,
                page_end,
                token_count,
                vector_literal,
            ),
        ).fetchone()


def set_document_node_parent_from_db(node_ids: list[int], parent_id: int) -> None:
    if not node_ids:
        return

    with open_database_connection_from_db() as database_connection:
        database_connection.execute(
            "UPDATE document_nodes SET parent_id = %s WHERE id = ANY(%s)",
            (parent_id, node_ids),
        )


def create_document_node_sources_from_db(summary_node_id: int, source_node_ids: list[int]) -> None:
    with open_database_connection_from_db() as database_connection:
        for source_node_id in source_node_ids:
            database_connection.execute(
                """
                INSERT INTO document_node_sources (summary_node_id, source_node_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (summary_node_id, source_node_id),
            )


def update_document_status_from_db(
    document_id: str,
    status: str,
    progress_percent: int,
    *,
    analysis_mode: str | None = None,
    summary: str | None = None,
    last_error: str | None = None,
) -> None:
    with open_database_connection_from_db() as database_connection:
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


def claim_document_analysis_job_from_db() -> dict | None:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            job = database_connection.execute(
                """
                SELECT id
                FROM document_analysis_jobs
                WHERE status IN ('queued', 'retrying') AND available_at <= NOW()
                ORDER BY available_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()

            if not job:
                return None

            return database_connection.execute(
                """
                UPDATE document_analysis_jobs
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    claimed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (job["id"],),
            ).fetchone()


def update_document_analysis_job_stage_from_db(job_id: int, stage: str) -> None:
    with open_database_connection_from_db() as database_connection:
        database_connection.execute(
            "UPDATE document_analysis_jobs SET stage = %s, updated_at = NOW() WHERE id = %s",
            (stage, job_id),
        )


def complete_document_analysis_job_from_db(job_id: int) -> None:
    with open_database_connection_from_db() as database_connection:
        database_connection.execute(
            """
            UPDATE document_analysis_jobs
            SET status = 'completed', stage = 'completed', completed_at = NOW(),
                updated_at = NOW(), last_error = ''
            WHERE id = %s
            """,
            (job_id,),
        )


def release_answer_jobs_waiting_for_document_from_db(document_id: str) -> int:
    with open_database_connection_from_db() as database_connection:
        result = database_connection.execute(
            """
            UPDATE answer_jobs
            SET status = 'queued', available_at = NOW(), updated_at = NOW()
            WHERE status = 'waiting_for_documents'
              AND id IN (
                  SELECT answer_job_documents.answer_job_id
                  FROM answer_job_documents
                  WHERE answer_job_documents.document_id = %s
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM answer_job_documents
                  JOIN documents ON documents.id = answer_job_documents.document_id
                  WHERE answer_job_documents.answer_job_id = answer_jobs.id
                    AND documents.status <> 'ready'
              )
            """,
            (document_id,),
        )
        return result.rowcount


def count_uncovered_nonblank_document_pages_from_db(document_id: str) -> int:
    with open_database_connection_from_db() as database_connection:
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


def retry_document_analysis_job_from_db(job_id: int, sanitized_error: str) -> dict | None:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            job = database_connection.execute(
                "SELECT id, document_id, attempt_count, max_attempts FROM document_analysis_jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()

            if not job:
                return None

            next_status = "failed" if job["attempt_count"] >= job["max_attempts"] else "retrying"
            updated_job = database_connection.execute(
                """
                UPDATE document_analysis_jobs
                SET status = %s,
                    available_at = CASE WHEN %s = 'retrying'
                        THEN NOW() + (LEAST(300, POWER(2, attempt_count) * 5)::TEXT || ' seconds')::INTERVAL
                        ELSE available_at END,
                    completed_at = CASE WHEN %s = 'failed' THEN NOW() ELSE NULL END,
                    last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (next_status, next_status, next_status, sanitized_error[:500], job_id),
            ).fetchone()

            if next_status == "failed":
                database_connection.execute(
                    """
                    UPDATE documents
                    SET status = 'failed',
                        progress_percent = 0,
                        last_error = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (sanitized_error[:500], job["document_id"]),
                )
                database_connection.execute(
                    """
                    UPDATE answer_jobs
                    SET status = 'failed',
                        completed_at = NOW(),
                        last_error = 'An attached document could not be analyzed',
                        updated_at = NOW()
                    WHERE status = 'waiting_for_documents'
                      AND id IN (
                          SELECT answer_job_documents.answer_job_id
                          FROM answer_job_documents
                          WHERE answer_job_documents.document_id = %s
                      )
                    """,
                    (job["document_id"],),
                )

            return updated_job


def enqueue_document_retry_from_db(document_id: str, user_id: int) -> dict | None:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            document = database_connection.execute(
                "SELECT id FROM documents WHERE id = %s AND user_id = %s FOR UPDATE",
                (document_id, user_id),
            ).fetchone()

            if not document:
                return None

            active_job = database_connection.execute(
                """
                SELECT *
                FROM document_analysis_jobs
                WHERE document_id = %s AND status IN ('queued', 'running', 'retrying')
                ORDER BY id DESC
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()

            if active_job:
                return active_job

            database_connection.execute(
                """
                UPDATE documents
                SET status = 'queued', progress_percent = 0, last_error = '', updated_at = NOW()
                WHERE id = %s
                """,
                (document_id,),
            )
            return database_connection.execute(
                "INSERT INTO document_analysis_jobs (document_id) VALUES (%s) RETURNING *",
                (document_id,),
            ).fetchone()


def delete_document_for_user_from_db(document_id: str, user_id: int) -> dict | None:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            document = database_connection.execute(
                "SELECT id, storage_name FROM documents WHERE id = %s AND user_id = %s FOR UPDATE",
                (document_id, user_id),
            ).fetchone()

            if not document:
                return None

            citation_pattern = (
                r"\[[^]]+\]\(/api/documents/"
                + document_id
                + r"/file\?user_id=[0-9]+#page=[0-9]+\)"
            )
            database_connection.execute(
                """
                UPDATE messages
                SET content = regexp_replace(
                    content,
                    %s,
                    '[unavailable source]',
                    'g'
                )
                WHERE role = 'assistant'
                  AND id IN (
                      SELECT message_id
                      FROM message_documents
                      WHERE document_id = %s
                  )
                """,
                (citation_pattern, document_id),
            )
            return database_connection.execute(
                "DELETE FROM documents WHERE id = %s RETURNING storage_name",
                (document_id,),
            ).fetchone()


def enqueue_answer_job_from_db(
    user_id: int,
    conversation_id: int,
    user_message_id: int,
    question: str,
    document_ids: list[str],
    wait_for_documents: bool,
) -> dict:
    initial_status = "waiting_for_documents" if wait_for_documents else "queued"

    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            answer_job = database_connection.execute(
                """
                INSERT INTO answer_jobs (
                    user_id, conversation_id, user_message_id, question, status
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (user_id, conversation_id, user_message_id, question, initial_status),
            ).fetchone()

            for document_id in document_ids:
                database_connection.execute(
                    """
                    INSERT INTO answer_job_documents (answer_job_id, document_id)
                    VALUES (%s, %s)
                    """,
                    (answer_job["id"], document_id),
                )

            return answer_job


def create_queued_document_question_from_db(
    user_id: int,
    conversation_id: int,
    question: str,
    document_ids: list[str],
    attach_document_ids: list[str] | None = None,
    message_content: str | None = None,
) -> tuple[dict, dict]:
    documents_to_attach = attach_document_ids or []

    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            conversation = database_connection.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = %s FOR UPDATE",
                (conversation_id, user_id),
            ).fetchone()

            if not conversation:
                raise ValueError("Conversation not found")

            owned_documents = database_connection.execute(
                """
                SELECT id, status
                FROM documents
                WHERE user_id = %s AND id = ANY(%s)
                ORDER BY id
                """,
                (user_id, document_ids),
            ).fetchall()

            if len(owned_documents) != len(set(document_ids)):
                raise ValueError("One or more documents are unavailable")

            failed_documents = [
                document
                for document in owned_documents
                if document["status"] in {"failed", "cancelled"}
            ]

            if failed_documents:
                raise ValueError("Retry or detach failed documents before asking a question")

            for document_id in documents_to_attach:
                database_connection.execute(
                    """
                    INSERT INTO conversation_documents (conversation_id, document_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (conversation_id, document_id),
                )

            persisted_content = message_content if message_content is not None else question
            user_message = database_connection.execute(
                """
                INSERT INTO messages (user_id, conversation_id, role, content)
                VALUES (%s, %s, 'user', %s)
                RETURNING id, conversation_id, role, content, created_at
                """,
                (user_id, conversation_id, persisted_content),
            ).fetchone()

            for document_id in document_ids:
                database_connection.execute(
                    """
                    INSERT INTO message_documents (message_id, document_id)
                    VALUES (%s, %s)
                    """,
                    (user_message["id"], document_id),
                )

            waits_for_documents = any(
                document["status"] != "ready" for document in owned_documents
            )
            initial_status = (
                "waiting_for_documents" if waits_for_documents else "queued"
            )
            answer_job = database_connection.execute(
                """
                INSERT INTO answer_jobs (
                    user_id, conversation_id, user_message_id, question, status
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    user_id,
                    conversation_id,
                    user_message["id"],
                    question,
                    initial_status,
                ),
            ).fetchone()

            for document_id in document_ids:
                database_connection.execute(
                    """
                    INSERT INTO answer_job_documents (answer_job_id, document_id)
                    VALUES (%s, %s)
                    """,
                    (answer_job["id"], document_id),
                )

            database_connection.execute(
                "UPDATE conversations SET updated_at = NOW() WHERE id = %s",
                (conversation_id,),
            )
            return user_message, answer_job


def get_answer_job_for_user_from_db(answer_job_id: int, user_id: int) -> dict | None:
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            """
            SELECT id, user_id, conversation_id, user_message_id, assistant_message_id,
                   status, last_error, created_at, updated_at
            FROM answer_jobs
            WHERE id = %s AND user_id = %s
            """,
            (answer_job_id, user_id),
        ).fetchone()


def claim_answer_job_from_db() -> dict | None:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            job = database_connection.execute(
                """
                SELECT id
                FROM answer_jobs
                WHERE status IN ('queued', 'retrying') AND available_at <= NOW()
                ORDER BY available_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()

            if not job:
                return None

            return database_connection.execute(
                """
                UPDATE answer_jobs
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    claimed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (job["id"],),
            ).fetchone()


def list_answer_job_documents_from_db(answer_job_id: int) -> list[dict]:
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {", ".join(f"documents.{column.strip()}" for column in DOCUMENT_COLUMNS.split(","))}
            FROM answer_job_documents
            JOIN documents ON documents.id = answer_job_documents.document_id
            WHERE answer_job_documents.answer_job_id = %s
            ORDER BY documents.id
            """,
            (answer_job_id,),
        ).fetchall()


def complete_answer_job_from_db(answer_job_id: int, assistant_message_id: int) -> None:
    with open_database_connection_from_db() as database_connection:
        database_connection.execute(
            """
            UPDATE answer_jobs
            SET status = 'completed',
                assistant_message_id = %s,
                completed_at = NOW(),
                last_error = '',
                updated_at = NOW()
            WHERE id = %s AND status = 'running'
            """,
            (assistant_message_id, answer_job_id),
        )


def save_and_complete_answer_job_from_db(
    answer_job_id: int,
    user_id: int,
    conversation_id: int,
    answer: str,
    document_ids: list[str],
    evidence_node_ids: list[int],
) -> dict | None:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            answer_job = database_connection.execute(
                """
                SELECT id
                FROM answer_jobs
                WHERE id = %s
                  AND user_id = %s
                  AND conversation_id = %s
                  AND status = 'running'
                  AND assistant_message_id IS NULL
                FOR UPDATE
                """,
                (answer_job_id, user_id, conversation_id),
            ).fetchone()

            if not answer_job:
                return None

            assistant_message = database_connection.execute(
                """
                INSERT INTO messages (user_id, conversation_id, role, content)
                VALUES (%s, %s, 'assistant', %s)
                RETURNING id, conversation_id, role, content, created_at
                """,
                (user_id, conversation_id, answer),
            ).fetchone()

            for document_id in document_ids:
                database_connection.execute(
                    """
                    INSERT INTO message_documents (message_id, document_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (assistant_message["id"], document_id),
                )

            for rank, evidence_node_id in enumerate(evidence_node_ids, start=1):
                database_connection.execute(
                    """
                    INSERT INTO answer_evidence (
                        assistant_message_id, document_node_id, rank
                    )
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (assistant_message["id"], evidence_node_id, rank),
                )

            database_connection.execute(
                """
                UPDATE answer_jobs
                SET status = 'completed',
                    assistant_message_id = %s,
                    completed_at = NOW(),
                    last_error = '',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (assistant_message["id"], answer_job_id),
            )
            database_connection.execute(
                "UPDATE conversations SET updated_at = NOW() WHERE id = %s",
                (conversation_id,),
            )
            return assistant_message


def retry_answer_job_from_db(answer_job_id: int, sanitized_error: str) -> dict | None:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            job = database_connection.execute(
                "SELECT id, attempt_count, max_attempts FROM answer_jobs WHERE id = %s FOR UPDATE",
                (answer_job_id,),
            ).fetchone()

            if not job:
                return None

            next_status = "failed" if job["attempt_count"] >= job["max_attempts"] else "retrying"
            return database_connection.execute(
                """
                UPDATE answer_jobs
                SET status = %s,
                    available_at = CASE WHEN %s = 'retrying'
                        THEN NOW() + (LEAST(300, POWER(2, attempt_count) * 5)::TEXT || ' seconds')::INTERVAL
                        ELSE available_at END,
                    completed_at = CASE WHEN %s = 'failed' THEN NOW() ELSE NULL END,
                    last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (next_status, next_status, next_status, sanitized_error[:500], answer_job_id),
            ).fetchone()


def release_stale_document_analysis_jobs_from_db(stale_after_seconds: int = 900) -> int:
    with open_database_connection_from_db() as database_connection:
        with database_connection.transaction():
            rows = database_connection.execute(
                """
                UPDATE document_analysis_jobs
                SET status = 'retrying',
                    available_at = NOW(),
                    claimed_at = NULL,
                    stage = 'worker_recovery',
                    last_error = 'worker_recovery',
                    updated_at = NOW()
                WHERE status = 'running'
                  AND claimed_at < NOW() - (%s::TEXT || ' seconds')::INTERVAL
                RETURNING document_id
                """,
                (stale_after_seconds,),
            ).fetchall()

            document_ids = [row["document_id"] for row in rows]

            if document_ids:
                database_connection.execute(
                    """
                    UPDATE documents
                    SET status = 'queued', updated_at = NOW()
                    WHERE id = ANY(%s)
                    """,
                    (document_ids,),
                )

            return len(rows)


def release_stale_answer_jobs_from_db(stale_after_seconds: int = 900) -> int:
    with open_database_connection_from_db() as database_connection:
        result = database_connection.execute(
            """
            UPDATE answer_jobs
            SET status = 'retrying',
                available_at = NOW(),
                claimed_at = NULL,
                last_error = 'worker_recovery',
                updated_at = NOW()
            WHERE status = 'running'
              AND assistant_message_id IS NULL
              AND claimed_at < NOW() - (%s::TEXT || ' seconds')::INTERVAL
            """,
            (stale_after_seconds,),
        )
        return result.rowcount


def list_document_nodes_by_vector_from_db(
    document_ids: list[str],
    node_types: list[str],
    query_embedding: list[float],
    limit: int,
) -> list[dict]:
    vector_literal = "[" + ",".join(str(value) for value in query_embedding) + "]"

    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            """
            SELECT document_nodes.id, document_nodes.document_id,
                   document_nodes.parent_id, document_nodes.node_type,
                   document_nodes.hierarchy_level, document_nodes.title,
                   document_nodes.content, document_nodes.page_start,
                   document_nodes.page_end, document_nodes.token_count,
                   documents.filename,
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


def list_document_nodes_by_lexical_search_from_db(
    document_ids: list[str],
    node_types: list[str],
    question: str,
    limit: int,
) -> list[dict]:
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            """
            SELECT document_nodes.id, document_nodes.document_id,
                   document_nodes.parent_id, document_nodes.node_type,
                   document_nodes.hierarchy_level, document_nodes.title,
                   document_nodes.content, document_nodes.page_start,
                   document_nodes.page_end, document_nodes.token_count,
                   documents.filename,
                   ts_rank_cd(
                       document_nodes.search_vector,
                       websearch_to_tsquery('english', %s)
                   ) AS score
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


def list_neighboring_document_nodes_from_db(
    document_id: str,
    page_start: int,
    page_end: int,
) -> list[dict]:
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            """
            SELECT document_nodes.id, document_nodes.document_id,
                   document_nodes.parent_id, document_nodes.node_type,
                   document_nodes.hierarchy_level, document_nodes.title,
                   document_nodes.content, document_nodes.page_start,
                   document_nodes.page_end, document_nodes.token_count,
                   documents.filename
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


def list_direct_document_pages_from_db(document_id: str) -> list[dict]:
    return list_document_pages_with_tables_from_db(document_id)


def save_answer_evidence_from_db(
    assistant_message_id: int,
    evidence_node_ids: list[int],
) -> None:
    with open_database_connection_from_db() as database_connection:
        for rank, evidence_node_id in enumerate(evidence_node_ids, start=1):
            database_connection.execute(
                """
                INSERT INTO answer_evidence (
                    assistant_message_id, document_node_id, rank
                )
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (assistant_message_id, evidence_node_id, rank),
            )


def list_previous_answer_evidence_ids_from_db(conversation_id: int) -> list[int]:
    with open_database_connection_from_db() as database_connection:
        rows = database_connection.execute(
            """
            SELECT answer_evidence.document_node_id
            FROM answer_evidence
            JOIN messages ON messages.id = answer_evidence.assistant_message_id
            WHERE messages.conversation_id = %s
            ORDER BY messages.id DESC, answer_evidence.rank
            LIMIT 20
            """,
            (conversation_id,),
        ).fetchall()
        return [row["document_node_id"] for row in rows]


def list_document_nodes_by_ids_from_db(
    document_ids: list[str],
    node_ids: list[int],
) -> list[dict]:
    if not node_ids:
        return []

    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            """
            SELECT document_nodes.id, document_nodes.document_id,
                   document_nodes.parent_id, document_nodes.node_type,
                   document_nodes.hierarchy_level, document_nodes.title,
                   document_nodes.content, document_nodes.page_start,
                   document_nodes.page_end, document_nodes.token_count,
                   documents.filename
            FROM document_nodes
            JOIN documents ON documents.id = document_nodes.document_id
            WHERE document_nodes.document_id = ANY(%s)
              AND document_nodes.id = ANY(%s)
            ORDER BY array_position(%s::bigint[], document_nodes.id)
            """,
            (document_ids, node_ids, node_ids),
        ).fetchall()


def list_all_document_leaf_nodes_from_db(document_id: str) -> list[dict]:
    with open_database_connection_from_db() as database_connection:
        return database_connection.execute(
            """
            SELECT document_nodes.id, document_nodes.document_id,
                   document_nodes.parent_id, document_nodes.node_type,
                   document_nodes.hierarchy_level, document_nodes.title,
                   document_nodes.content, document_nodes.page_start,
                   document_nodes.page_end, document_nodes.token_count,
                   documents.filename
            FROM document_nodes
            JOIN documents ON documents.id = document_nodes.document_id
            WHERE document_nodes.document_id = %s
              AND document_nodes.node_type IN ('evidence', 'table')
            ORDER BY document_nodes.page_start, document_nodes.id
            """,
            (document_id,),
        ).fetchall()
