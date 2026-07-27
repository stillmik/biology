from contextlib import contextmanager
import logging
import os
from time import perf_counter
from typing import Iterator

import psycopg


from psycopg.errors import DuplicateColumn
from psycopg.rows import dict_row
from .observability import DB_CONNECTION_FAILURES, DB_LOCK_WAIT_DURATION, log_event, observe_database_operation


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://biology:biology@db:5432/biology_chat")


def open_database_connection_from_db() -> psycopg.Connection:
    try:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    except Exception:
        DB_CONNECTION_FAILURES.inc()
        log_event(logging.getLogger("biology.database"), logging.ERROR, "database_connection_failed")
        raise


@observe_database_operation("initialize_database_from_db")
def initialize_database_from_db() -> None:
    with open_database_connection_from_db() as database_connection:
        db = database_connection
        db.execute("CREATE TABLE IF NOT EXISTS users (id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS conversations (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL DEFAULT 'New conversation', created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS messages (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id), role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        try:
            db.execute("ALTER TABLE messages ADD COLUMN conversation_id BIGINT REFERENCES conversations(id) ON DELETE CASCADE")
        except DuplicateColumn:
            db.rollback()

        db.execute("CREATE TABLE IF NOT EXISTS conversation_summaries (id BIGSERIAL PRIMARY KEY, conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, content TEXT NOT NULL, token_count INTEGER NOT NULL, covered_until_message_id BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS conversation_summary_segments (id BIGSERIAL PRIMARY KEY, conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, content TEXT NOT NULL, token_count INTEGER NOT NULL, covered_from_message_id BIGINT NOT NULL, covered_until_message_id BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, CHECK (covered_from_message_id <= covered_until_message_id), UNIQUE (conversation_id, covered_from_message_id, covered_until_message_id))")
        db.execute("CREATE TABLE IF NOT EXISTS summary_jobs (id BIGSERIAL PRIMARY KEY, conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, source_message_id BIGINT REFERENCES messages(id) ON DELETE SET NULL, source_trace_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')), attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, claimed_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE INDEX IF NOT EXISTS messages_conversation_id_id_index ON messages (conversation_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS summary_segments_conversation_id_covered_until_index ON conversation_summary_segments (conversation_id, covered_until_message_id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS summary_jobs_ready_index ON summary_jobs (status, available_at, id)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS summary_jobs_one_active_per_conversation ON summary_jobs (conversation_id) WHERE status IN ('queued', 'running')")
        db.execute("INSERT INTO conversations (user_id, title) SELECT id, 'New conversation' FROM users WHERE NOT EXISTS (SELECT 1 FROM conversations WHERE conversations.user_id = users.id)")
        db.execute("UPDATE messages SET conversation_id = (SELECT id FROM conversations WHERE conversations.user_id = messages.user_id ORDER BY id LIMIT 1) WHERE conversation_id IS NULL")


@observe_database_operation("create_user_from_db")
def create_user_from_db(username: str) -> dict:
    with open_database_connection_from_db() as db:
        db.execute("INSERT INTO users (username) VALUES (%s)", (username,))
        return db.execute("SELECT id, username FROM users WHERE username = %s", (username,)).fetchone()


@observe_database_operation("get_user_from_db")
def get_user_from_db(user_id: int) -> dict | None:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, username FROM users WHERE id = %s", (user_id,)).fetchone()


@observe_database_operation("get_user_by_username_from_db")
def get_user_by_username_from_db(username: str) -> dict | None:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, username FROM users WHERE username = %s", (username,)).fetchone()


@observe_database_operation("list_user_messages_from_db")
def list_user_messages_from_db(user_id: int) -> list[dict]:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, role, content, created_at FROM messages WHERE user_id = %s ORDER BY id", (user_id,)).fetchall()


@observe_database_operation("create_conversation_from_db")
def create_conversation_from_db(user_id: int, title: str = "New conversation") -> dict:
    with open_database_connection_from_db() as db:
        return db.execute("INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id, user_id, title, created_at, updated_at", (user_id, title)).fetchone()


@observe_database_operation("list_user_conversations_from_db")
def list_user_conversations_from_db(user_id: int) -> list[dict]:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, user_id, title, created_at, updated_at FROM conversations WHERE user_id = %s ORDER BY updated_at DESC, id DESC", (user_id,)).fetchall()


@observe_database_operation("get_conversation_from_db")
def get_conversation_from_db(conversation_id: int, user_id: int) -> dict | None:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, user_id, title, created_at, updated_at FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id)).fetchone()


@observe_database_operation("list_conversation_messages_from_db")
def list_conversation_messages_from_db(conversation_id: int) -> list[dict]:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, role, content, created_at FROM messages WHERE conversation_id = %s ORDER BY id", (conversation_id,)).fetchall()


@observe_database_operation("list_recent_conversation_messages_from_db")
def list_recent_conversation_messages_from_db(conversation_id: int, limit: int) -> list[dict]:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, role, content, created_at FROM messages WHERE conversation_id = %s ORDER BY id DESC LIMIT %s", (conversation_id, limit)).fetchall()[::-1]


@observe_database_operation("list_conversation_messages_after_from_db")
def list_conversation_messages_after_from_db(conversation_id: int, message_id: int = 0) -> list[dict]:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, role, content, created_at FROM messages WHERE conversation_id = %s AND id > %s ORDER BY id", (conversation_id, message_id)).fetchall()


@observe_database_operation("get_latest_summary_from_db")
def get_latest_summary_from_db(conversation_id: int) -> dict | None:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, conversation_id, content, token_count, covered_until_message_id, created_at FROM conversation_summaries WHERE conversation_id = %s ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()


@observe_database_operation("list_conversation_summaries_from_db")
def list_conversation_summaries_from_db(conversation_id: int) -> list[dict]:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, conversation_id, content, token_count, covered_until_message_id, created_at FROM conversation_summaries WHERE conversation_id = %s ORDER BY id DESC", (conversation_id,)).fetchall()


@observe_database_operation("create_summary_from_db")
def create_summary_from_db(conversation_id: int, content: str, token_count: int, covered_until_message_id: int) -> dict:
    with open_database_connection_from_db() as db:
        return db.execute("INSERT INTO conversation_summaries (conversation_id, content, token_count, covered_until_message_id) VALUES (%s, %s, %s, %s) RETURNING id, conversation_id, content, token_count, covered_until_message_id, created_at", (conversation_id, content, token_count, covered_until_message_id)).fetchone()


@observe_database_operation("get_latest_summary_segment_from_db")
def get_latest_summary_segment_from_db(conversation_id: int) -> dict | None:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT id, conversation_id, content, token_count, covered_from_message_id, covered_until_message_id, created_at FROM conversation_summary_segments WHERE conversation_id = %s ORDER BY covered_until_message_id DESC, id DESC LIMIT 1", (conversation_id,)).fetchone()


@observe_database_operation("list_recent_summary_segments_within_token_budget_from_db")
def list_recent_summary_segments_within_token_budget_from_db(conversation_id: int, token_budget: int) -> list[dict]:
    with open_database_connection_from_db() as db:
        newest_first = db.execute("SELECT id, conversation_id, content, token_count, covered_from_message_id, covered_until_message_id, created_at FROM conversation_summary_segments WHERE conversation_id = %s ORDER BY covered_until_message_id DESC, id DESC", (conversation_id,)).fetchall()
    selected, used_tokens = [], 0
    for segment in newest_first:
        if used_tokens + segment["token_count"] > token_budget:
            break
        selected.append(segment)
        used_tokens += segment["token_count"]
    return list(reversed(selected))


@observe_database_operation("create_summary_segment_from_db")
def create_summary_segment_from_db(conversation_id: int, content: str, token_count: int, covered_from_message_id: int, covered_until_message_id: int) -> dict:
    with open_database_connection_from_db() as db:
        return db.execute("INSERT INTO conversation_summary_segments (conversation_id, content, token_count, covered_from_message_id, covered_until_message_id) VALUES (%s, %s, %s, %s, %s) RETURNING id, conversation_id, content, token_count, covered_from_message_id, covered_until_message_id, created_at", (conversation_id, content, token_count, covered_from_message_id, covered_until_message_id)).fetchone()


@observe_database_operation("create_message_from_db")
def create_message_from_db(user_id: int, conversation_id: int, role: str, content: str) -> dict:
    with open_database_connection_from_db() as db:
        message = db.execute("INSERT INTO messages (user_id, conversation_id, role, content) VALUES (%s, %s, %s, %s) RETURNING id, conversation_id, role, content, created_at", (user_id, conversation_id, role, content)).fetchone()
        db.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s AND user_id = %s", (conversation_id, user_id))
        return message


@observe_database_operation("enqueue_summary_job_from_db")
def enqueue_summary_job_from_db(conversation_id: int, source_message_id: int | None, source_trace_id: str = "") -> dict:
    with open_database_connection_from_db() as db:
        with db.transaction():
            active_job = db.execute("SELECT id, conversation_id, source_message_id, source_trace_id, status, attempt_count, max_attempts, available_at, claimed_at, completed_at, last_error, created_at, updated_at FROM summary_jobs WHERE conversation_id = %s AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1 FOR UPDATE", (conversation_id,)).fetchone()
            if active_job:
                if active_job["status"] == "queued":
                    return db.execute("UPDATE summary_jobs SET source_message_id = %s, source_trace_id = %s, available_at = NOW(), updated_at = NOW() WHERE id = %s RETURNING id, conversation_id, source_message_id, source_trace_id, status, attempt_count, max_attempts, available_at, claimed_at, completed_at, last_error, created_at, updated_at", (source_message_id, source_trace_id, active_job["id"])).fetchone()
                return active_job
            return db.execute("INSERT INTO summary_jobs (conversation_id, source_message_id, source_trace_id) VALUES (%s, %s, %s) RETURNING id, conversation_id, source_message_id, source_trace_id, status, attempt_count, max_attempts, available_at, claimed_at, completed_at, last_error, created_at, updated_at", (conversation_id, source_message_id, source_trace_id)).fetchone()


@observe_database_operation("claim_summary_job_from_db")
def claim_summary_job_from_db() -> dict | None:
    with open_database_connection_from_db() as db:
        with db.transaction():
            job = db.execute("SELECT id FROM summary_jobs WHERE status = 'queued' AND available_at <= NOW() ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
            if not job:
                return None
            return db.execute("UPDATE summary_jobs SET status = 'running', attempt_count = attempt_count + 1, claimed_at = NOW(), updated_at = NOW() WHERE id = %s RETURNING id, conversation_id, source_message_id, source_trace_id, status, attempt_count, max_attempts, available_at, claimed_at, completed_at, last_error, created_at, updated_at", (job["id"],)).fetchone()


@observe_database_operation("complete_summary_job_from_db")
def complete_summary_job_from_db(job_id: int) -> None:
    with open_database_connection_from_db() as db:
        db.execute("UPDATE summary_jobs SET status = 'completed', completed_at = NOW(), updated_at = NOW(), last_error = '' WHERE id = %s", (job_id,))


@observe_database_operation("retry_summary_job_from_db")
def retry_summary_job_from_db(job_id: int, sanitized_error: str) -> dict | None:
    with open_database_connection_from_db() as db:
        with db.transaction():
            job = db.execute("SELECT id, attempt_count, max_attempts FROM summary_jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
            if not job:
                return None
            status = "failed" if job["attempt_count"] >= job["max_attempts"] else "queued"
            return db.execute("UPDATE summary_jobs SET status = %s, available_at = CASE WHEN %s = 'queued' THEN NOW() + (LEAST(300, POWER(2, attempt_count) * 5)::TEXT || ' seconds')::INTERVAL ELSE available_at END, completed_at = CASE WHEN %s = 'failed' THEN NOW() ELSE NULL END, updated_at = NOW(), last_error = %s WHERE id = %s RETURNING id, conversation_id, source_message_id, source_trace_id, status, attempt_count, max_attempts, available_at, claimed_at, completed_at, last_error, created_at, updated_at", (status, status, status, sanitized_error[:500], job_id)).fetchone()


@observe_database_operation("cancel_pending_summary_jobs_for_conversation_from_db")
def cancel_pending_summary_jobs_for_conversation_from_db(conversation_id: int) -> int:
    with open_database_connection_from_db() as db:
        result = db.execute("UPDATE summary_jobs SET status = 'cancelled', completed_at = NOW(), updated_at = NOW() WHERE conversation_id = %s AND status = 'queued'", (conversation_id,))
        return result.rowcount


@observe_database_operation("list_summary_job_counts_from_db")
def list_summary_job_counts_from_db() -> list[dict]:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT status, COUNT(*) AS count FROM summary_jobs GROUP BY status").fetchall()


@observe_database_operation("release_stale_summary_jobs_from_db")
def release_stale_summary_jobs_from_db(stale_after_seconds: int = 900) -> int:
    with open_database_connection_from_db() as db:
        result = db.execute("UPDATE summary_jobs SET status = 'queued', available_at = NOW(), claimed_at = NULL, updated_at = NOW(), last_error = 'worker_recovery' WHERE status = 'running' AND claimed_at < NOW() - (%s::TEXT || ' seconds')::INTERVAL", (stale_after_seconds,))
        return result.rowcount


@observe_database_operation("update_conversation_title_from_db")
def update_conversation_title_from_db(conversation_id: int, user_id: int, title: str) -> dict | None:
    with open_database_connection_from_db() as db:
        return db.execute("UPDATE conversations SET title = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND user_id = %s RETURNING id, user_id, title, created_at, updated_at", (title, conversation_id, user_id)).fetchone()


@observe_database_operation("get_message_for_user_from_db")
def get_message_for_user_from_db(message_id: int, user_id: int) -> dict | None:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT messages.id, messages.conversation_id, messages.role, messages.content FROM messages JOIN conversations ON conversations.id = messages.conversation_id WHERE messages.id = %s AND conversations.user_id = %s", (message_id, user_id)).fetchone()


@observe_database_operation("update_message_content_from_db")
def update_message_content_from_db(message_id: int, user_id: int, content: str) -> dict | None:
    with open_database_connection_from_db() as db:
        message = db.execute("SELECT conversation_id FROM messages WHERE id = %s AND conversation_id IN (SELECT id FROM conversations WHERE user_id = %s)", (message_id, user_id)).fetchone()
        if not message:
            return None
        db.execute("DELETE FROM messages WHERE conversation_id = %s AND id > %s", (message["conversation_id"], message_id))
        return db.execute("UPDATE messages SET content = %s WHERE id = %s RETURNING id, conversation_id, role, content, created_at", (content, message_id)).fetchone()


@observe_database_operation("delete_message_from_db")
def delete_message_from_db(message_id: int, user_id: int) -> bool:
    with open_database_connection_from_db() as db:
        with db.transaction():
            message = db.execute("SELECT m.conversation_id FROM messages AS m JOIN conversations AS c ON c.id = m.conversation_id WHERE m.id = %s AND c.user_id = %s FOR UPDATE", (message_id, user_id)).fetchone()
            if not message:
                return False
            db.execute("DELETE FROM conversation_summary_segments WHERE conversation_id = %s AND covered_until_message_id >= %s", (message["conversation_id"], message_id))
            db.execute("UPDATE summary_jobs SET status = 'cancelled', completed_at = NOW(), updated_at = NOW() WHERE conversation_id = %s AND status = 'queued'", (message["conversation_id"],))
            db.execute("DELETE FROM messages WHERE id = %s", (message_id,))
            db.execute("UPDATE conversations SET updated_at = NOW() WHERE id = %s", (message["conversation_id"],))
            return True


@observe_database_operation("delete_conversation_from_db")
def delete_conversation_from_db(conversation_id: int, user_id: int) -> bool:
    with open_database_connection_from_db() as db:
        result = db.execute("DELETE FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id))
        return result.rowcount > 0


@observe_database_operation("get_user_message_from_db")
def get_user_message_from_db(message_id: int, user_id: int) -> dict | None:
    with open_database_connection_from_db() as db:
        return db.execute("SELECT m.id, m.conversation_id, m.role, m.content, m.created_at FROM messages AS m JOIN conversations AS c ON c.id = m.conversation_id WHERE m.id = %s AND c.user_id = %s", (message_id, user_id)).fetchone()


@observe_database_operation("count_messages_after_from_db")
def count_messages_after_from_db(conversation_id: int, message_id: int) -> int:
    with open_database_connection_from_db() as db:
        row = db.execute("SELECT COUNT(*) AS count FROM messages WHERE conversation_id = %s AND id > %s", (conversation_id, message_id)).fetchone()
        return int(row["count"])


@observe_database_operation("update_user_message_and_delete_following_from_db")
def update_user_message_and_delete_following_from_db(message_id: int, user_id: int, new_content: str) -> dict | None:
    """
    Edit one user message and erase the conversation timeline after it.

    Any summary segment covering the edited message is invalidated because it
    was generated from the old content.
    """

    with open_database_connection_from_db() as db:
        with db.transaction():
            message = db.execute("SELECT m.id, m.conversation_id, m.role FROM messages AS m JOIN conversations AS c ON c.id = m.conversation_id WHERE m.id = %s AND c.user_id = %s FOR UPDATE", (message_id, user_id)).fetchone()

            if not message:
                return None

            if message["role"] != "user":
                raise ValueError("Only user messages can be edited")

            conversation_id = message["conversation_id"]

            updated_message = db.execute("UPDATE messages SET content = %s WHERE id = %s RETURNING id, conversation_id, role, content, created_at", (new_content, message_id)).fetchone()
            # Delete every message after the edited point.
            db.execute("DELETE FROM messages WHERE conversation_id = %s AND id > %s", (conversation_id, message_id))

            db.execute("DELETE FROM conversation_summary_segments WHERE conversation_id = %s AND covered_until_message_id >= %s", (conversation_id, message_id))
            db.execute("UPDATE summary_jobs SET status = 'cancelled', completed_at = NOW(), updated_at = NOW() WHERE conversation_id = %s AND status = 'queued'", (conversation_id,))
            db.execute("UPDATE conversations SET updated_at = NOW() WHERE id = %s", (conversation_id,))

    return updated_message


@observe_database_operation("list_conversation_messages_page_from_db")
def list_conversation_messages_page_from_db(conversation_id: int, limit: int, before_id: int | None) -> list[dict]:
    with open_database_connection_from_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, role, content, created_at FROM messages WHERE conversation_id = %s AND (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT %s", (conversation_id, before_id, before_id, limit))
            rows = cursor.fetchall()
    return list(reversed(rows))


@contextmanager
def lock_conversation_from_db(conversation_id: int) -> Iterator[None]:
    with open_database_connection_from_db() as connection:
        started = perf_counter()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (conversation_id,))
        DB_LOCK_WAIT_DURATION.observe(perf_counter() - started)
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (conversation_id,))
