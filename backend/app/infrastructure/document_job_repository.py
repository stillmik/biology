from .database import open_database_connection
from .document_repository import qualify_document_columns


def claim_document_analysis_job_from_db() -> dict | None:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            job = database_connection.execute("SELECT id FROM document_analysis_jobs WHERE status IN ('queued', 'retrying') AND available_at <= NOW() ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()

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


def update_document_analysis_job_stage_db(job_id: int, stage: str) -> None:
    with open_database_connection() as database_connection:
        database_connection.execute("UPDATE document_analysis_jobs SET stage = %s, updated_at = NOW() WHERE id = %s", (stage, job_id))


def complete_document_analysis_job_db(job_id: int) -> None:
    with open_database_connection() as database_connection:
        database_connection.execute(
            """
            UPDATE document_analysis_jobs
            SET status = 'completed', stage = 'completed', completed_at = NOW(),
                updated_at = NOW(), last_error = ''
            WHERE id = %s
            """,
            (job_id,),
        )


def release_answer_jobs_waiting_for_document_db(document_id: str) -> int:
    with open_database_connection() as database_connection:
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


def retry_document_analysis_job_from_db(job_id: int, sanitized_error: str, retryable: bool = True) -> dict | None:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            job = database_connection.execute("SELECT id, document_id, status, attempt_count, max_attempts FROM document_analysis_jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()

            if not job:
                return None

            if job["status"] == "completed":
                return job

            attempts_exhausted = job["attempt_count"] >= job["max_attempts"]
            next_status = "retrying" if retryable and not attempts_exhausted else "failed"
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

            if next_status != "failed":
                return updated_job

            answer_job_error = f"An attached document could not be analyzed: {sanitized_error}"[:500]
            database_connection.execute(
                """
                UPDATE documents
                SET status = 'failed', progress_percent = 0,
                    last_error = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (sanitized_error[:500], job["document_id"]),
            )
            database_connection.execute(
                """
                UPDATE answer_jobs
                SET status = 'failed', completed_at = NOW(),
                    last_error = %s,
                    updated_at = NOW()
                WHERE status = 'waiting_for_documents'
                  AND id IN (
                      SELECT answer_job_documents.answer_job_id
                      FROM answer_job_documents
                      WHERE answer_job_documents.document_id = %s
                  )
                """,
                (answer_job_error, job["document_id"]),
            )
            return updated_job


def enqueue_document_retry_from_db(document_id: str, user_id: int) -> dict | None:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            document = database_connection.execute("SELECT id FROM documents WHERE id = %s AND user_id = %s FOR UPDATE", (document_id, user_id)).fetchone()

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

            previous_job = database_connection.execute("SELECT stage FROM document_analysis_jobs WHERE document_id = %s ORDER BY id DESC LIMIT 1", (document_id,)).fetchone()
            resume_stage = previous_job["stage"] if previous_job and previous_job["stage"] in {"extracted", "indexed", "summarized"} else "queued"
            database_connection.execute("UPDATE documents SET status = 'queued', progress_percent = 0, last_error = '', updated_at = NOW() WHERE id = %s", (document_id,))
            return database_connection.execute("INSERT INTO document_analysis_jobs (document_id, stage) VALUES (%s, %s) RETURNING *", (document_id, resume_stage)).fetchone()


def _validate_document_question_request_in_db(database_connection, user_id: int, conversation_id: int, document_ids: list[str]) -> list[dict]:
    conversation = database_connection.execute("SELECT id FROM conversations WHERE id = %s AND user_id = %s FOR UPDATE", (conversation_id, user_id)).fetchone()
    if not conversation:
        raise ValueError("Conversation not found")

    owned_documents = database_connection.execute("SELECT id, status FROM documents WHERE user_id = %s AND id = ANY(%s) ORDER BY id", (user_id, document_ids)).fetchall()
    if len(owned_documents) != len(set(document_ids)):
        raise ValueError("One or more documents are unavailable")

    failed_documents = [document for document in owned_documents if document["status"] in {"failed", "cancelled"}]
    if failed_documents:
        raise ValueError("Retry or detach failed documents before asking a question")
    return owned_documents


def _attach_documents_to_conversation_in_db(database_connection, conversation_id: int, document_ids: list[str]) -> None:
    for document_id in document_ids:
        database_connection.execute("INSERT INTO conversation_documents (conversation_id, document_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (conversation_id, document_id))


def _create_document_question_message_in_db(database_connection, user_id: int, conversation_id: int, question: str, document_ids: list[str], message_content: str | None) -> dict:
    persisted_content = message_content if message_content is not None else question
    user_message = database_connection.execute("INSERT INTO messages (user_id, conversation_id, role, content) VALUES (%s, %s, 'user', %s) RETURNING id, conversation_id, role, content, created_at", (user_id, conversation_id, persisted_content)).fetchone()
    for document_id in document_ids:
        database_connection.execute("INSERT INTO message_documents (message_id, document_id) VALUES (%s, %s)", (user_message["id"], document_id))
    return user_message


def _create_document_answer_job_in_db(database_connection, user_id: int, conversation_id: int, question: str, user_message: dict, document_ids: list[str], owned_documents: list[dict]) -> dict:
    waits_for_documents = any(document["status"] != "ready" for document in owned_documents)
    initial_status = "waiting_for_documents" if waits_for_documents else "queued"
    answer_job = database_connection.execute("INSERT INTO answer_jobs (user_id, conversation_id, user_message_id, question, status) VALUES (%s, %s, %s, %s, %s) RETURNING *", (user_id, conversation_id, user_message["id"], question, initial_status)).fetchone()
    for document_id in document_ids:
        database_connection.execute("INSERT INTO answer_job_documents (answer_job_id, document_id) VALUES (%s, %s)", (answer_job["id"], document_id))
    return answer_job


def create_answer_job_and_attach_conversation_documents_in_db(user_id: int, conversation_id: int, question: str, document_ids: list[str], attach_document_ids: list[str] | None = None, message_content: str | None = None) -> tuple[dict, dict]:
    """Atomically persist a document question and its queued answer job."""
    documents_to_attach = attach_document_ids or []

    with open_database_connection() as database_connection:
        with database_connection.transaction():
            owned_documents = _validate_document_question_request_in_db(database_connection, user_id, conversation_id, document_ids)
            _attach_documents_to_conversation_in_db(database_connection, conversation_id, documents_to_attach)
            user_message = _create_document_question_message_in_db(database_connection, user_id, conversation_id, question, document_ids, message_content)
            answer_job = _create_document_answer_job_in_db(database_connection, user_id, conversation_id, question, user_message, document_ids, owned_documents)

            database_connection.execute("UPDATE conversations SET updated_at = NOW() WHERE id = %s", (conversation_id))
            return user_message, answer_job


def get_answer_job_for_user_from_db(answer_job_id: int, user_id: int) -> dict | None:
    with open_database_connection() as database_connection:
        return database_connection.execute(
            """
            SELECT id, user_id, conversation_id, user_message_id,
                   assistant_message_id, answer_depth, status, last_error,
                   created_at, updated_at
            FROM answer_jobs
            WHERE id = %s AND user_id = %s
            """,
            (answer_job_id, user_id),
        ).fetchone()


def get_previous_document_answer_depth_db(conversation_id: int) -> str | None:
    with open_database_connection() as database_connection:
        row = database_connection.execute(
            """
            SELECT answer_depth
            FROM answer_jobs
            WHERE conversation_id = %s
              AND status = 'completed'
              AND answer_depth IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        return row["answer_depth"] if row else None


def claim_answer_job_db() -> dict | None:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            job = database_connection.execute("SELECT id FROM answer_jobs WHERE status IN ('queued', 'retrying') AND available_at <= NOW() AND assistant_message_id IS NULL ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()

            if not job:
                return None

            return database_connection.execute("UPDATE answer_jobs SET status = 'running', attempt_count = attempt_count + 1, claimed_at = NOW(), updated_at = NOW() WHERE id = %s RETURNING *", (job["id"],)).fetchone()


def list_answer_job_documents_from_db(answer_job_id: int) -> list[dict]:
    qualified_columns = qualify_document_columns("documents")

    with open_database_connection() as database_connection:
        return database_connection.execute(
            f"""
            SELECT {qualified_columns}
            FROM answer_job_documents
            JOIN documents ON documents.id = answer_job_documents.document_id
            WHERE answer_job_documents.answer_job_id = %s
            ORDER BY documents.id
            """,
            (answer_job_id,),
        ).fetchall()


def save_and_complete_answer_job_db(answer_job_id: int, user_id: int, conversation_id: int, answer: str, answer_depth: str, document_ids: list[str], source_ids: list[int]) -> dict | None:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            answer_job = database_connection.execute(
                """
                SELECT id
                FROM answer_jobs
                WHERE id = %s AND user_id = %s AND conversation_id = %s
                  AND status = 'running' AND assistant_message_id IS NULL
                FOR UPDATE
                """,
                (answer_job_id, user_id, conversation_id),
            ).fetchone()

            if not answer_job:
                return None

            assistant_message = database_connection.execute("INSERT INTO messages (user_id, conversation_id, role, content) VALUES (%s, %s, 'assistant', %s) RETURNING id, conversation_id, role, content, created_at", (user_id, conversation_id, answer)).fetchone()

            for document_id in document_ids:
                database_connection.execute("INSERT INTO message_documents (message_id, document_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (assistant_message["id"], document_id))

            for rank, source_id in enumerate(source_ids, start=1):
                database_connection.execute("INSERT INTO answer_evidence (assistant_message_id, source_id, rank) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (assistant_message["id"], source_id, rank))

            database_connection.execute("UPDATE answer_jobs SET status = 'completed', assistant_message_id = %s, answer_depth = %s, completed_at = NOW(), last_error = '', updated_at = NOW() WHERE id = %s", (assistant_message["id"], answer_depth, answer_job_id))
            database_connection.execute("UPDATE conversations SET updated_at = NOW() WHERE id = %s", (conversation_id,))
            return assistant_message


def retry_answer_job_from_db(answer_job_id: int, sanitized_error: str) -> dict | None:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            job = database_connection.execute("SELECT id, status, assistant_message_id, attempt_count, max_attempts FROM answer_jobs WHERE id = %s FOR UPDATE", (answer_job_id,)).fetchone()

            if not job:
                return None

            if job["assistant_message_id"] is not None or job["status"] == "completed":
                return job

            next_status = "failed" if job["attempt_count"] >= job["max_attempts"] else "retrying"
            return database_connection.execute(
                """
                UPDATE answer_jobs
                SET status = %s,
                    available_at = CASE WHEN %s = 'retrying'
                        THEN NOW() + (LEAST(300, POWER(2, attempt_count) * 5)::TEXT || ' seconds')::INTERVAL
                        ELSE available_at END,
                    completed_at = CASE WHEN %s = 'failed' THEN NOW() ELSE NULL END,
                    last_error = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (next_status, next_status, next_status, sanitized_error[:500], answer_job_id),
            ).fetchone()


def release_stale_document_analysis_jobs(stale_after_seconds: int = 900) -> int:
    with open_database_connection() as database_connection:
        with database_connection.transaction():
            rows = database_connection.execute(
                """
                UPDATE document_analysis_jobs
                SET status = 'retrying', available_at = NOW(),
                    claimed_at = NULL, last_error = 'worker_recovery',
                    updated_at = NOW()
                WHERE status = 'running'
                  AND claimed_at < NOW() - (%s::TEXT || ' seconds')::INTERVAL
                RETURNING document_id
                """,
                (stale_after_seconds,),
            ).fetchall()
            document_ids = [row["document_id"] for row in rows]

            if document_ids:
                database_connection.execute("UPDATE documents SET status = 'queued', updated_at = NOW() WHERE id = ANY(%s)", (document_ids,))

            return len(rows)


def release_stale_answer_jobs_from_db(stale_after_seconds: int = 900) -> int:
    with open_database_connection() as database_connection:
        result = database_connection.execute(
            """
            UPDATE answer_jobs
            SET status = 'retrying', available_at = NOW(), claimed_at = NULL,
                last_error = 'worker_recovery', updated_at = NOW()
            WHERE status = 'running'
              AND assistant_message_id IS NULL
              AND claimed_at < NOW() - (%s::TEXT || ' seconds')::INTERVAL
            """,
            (stale_after_seconds,),
        )
        return result.rowcount
