import os

import psycopg
from psycopg.errors import DuplicateColumn
from psycopg.rows import dict_row


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://biology:biology@db:5432/biology_chat")


def connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db() -> None:
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS users (id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS conversations (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL DEFAULT 'New conversation', created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS messages (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id), role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        try:
            db.execute("ALTER TABLE messages ADD COLUMN conversation_id BIGINT REFERENCES conversations(id) ON DELETE CASCADE")
        except DuplicateColumn:
            db.rollback()
        db.execute("INSERT INTO conversations (user_id, title) SELECT id, 'New conversation' FROM users WHERE NOT EXISTS (SELECT 1 FROM conversations WHERE conversations.user_id = users.id)")
        db.execute("UPDATE messages SET conversation_id = (SELECT id FROM conversations WHERE conversations.user_id = messages.user_id ORDER BY id LIMIT 1) WHERE conversation_id IS NULL")


def create_user(username: str) -> dict:
    with connection() as db:
        db.execute("INSERT INTO users (username) VALUES (%s)", (username,))
        return db.execute("SELECT id, username FROM users WHERE username = %s", (username,)).fetchone()


def get_user(user_id: int) -> dict | None:
    with connection() as db:
        return db.execute("SELECT id, username FROM users WHERE id = %s", (user_id,)).fetchone()


def get_user_by_username(username: str) -> dict | None:
    with connection() as db:
        return db.execute("SELECT id, username FROM users WHERE username = %s", (username,)).fetchone()


def get_messages(user_id: int) -> list[dict]:
    with connection() as db:
        return db.execute("SELECT id, role, content, created_at FROM messages WHERE user_id = %s ORDER BY id", (user_id,)).fetchall()


def create_conversation(user_id: int, title: str = "New conversation") -> dict:
    with connection() as db:
        return db.execute("INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id, user_id, title, created_at, updated_at", (user_id, title)).fetchone()


def get_conversations(user_id: int) -> list[dict]:
    with connection() as db:
        return db.execute("SELECT id, user_id, title, created_at, updated_at FROM conversations WHERE user_id = %s ORDER BY updated_at DESC, id DESC", (user_id,)).fetchall()


def get_conversation(conversation_id: int, user_id: int) -> dict | None:
    with connection() as db:
        return db.execute("SELECT id, user_id, title, created_at, updated_at FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id)).fetchone()


def get_conversation_messages(conversation_id: int) -> list[dict]:
    with connection() as db:
        return db.execute("SELECT id, role, content, created_at FROM messages WHERE conversation_id = %s ORDER BY id", (conversation_id,)).fetchall()


def add_message(user_id: int, conversation_id: int, role: str, content: str) -> None:
    with connection() as db:
        db.execute("INSERT INTO messages (user_id, conversation_id, role, content) VALUES (%s, %s, %s, %s)", (user_id, conversation_id, role, content))
        db.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s AND user_id = %s", (conversation_id, user_id))


def rename_conversation(conversation_id: int, user_id: int, title: str) -> dict | None:
    with connection() as db:
        return db.execute("UPDATE conversations SET title = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND user_id = %s RETURNING id, user_id, title, created_at, updated_at", (title, conversation_id, user_id)).fetchone()


def get_message(message_id: int, user_id: int) -> dict | None:
    with connection() as db:
        return db.execute("SELECT messages.id, messages.conversation_id, messages.role, messages.content FROM messages JOIN conversations ON conversations.id = messages.conversation_id WHERE messages.id = %s AND conversations.user_id = %s", (message_id, user_id)).fetchone()


def update_message(message_id: int, user_id: int, content: str) -> dict | None:
    with connection() as db:
        message = db.execute("SELECT conversation_id FROM messages WHERE id = %s AND conversation_id IN (SELECT id FROM conversations WHERE user_id = %s)", (message_id, user_id)).fetchone()
        if not message:
            return None
        db.execute("DELETE FROM messages WHERE conversation_id = %s AND id > %s", (message["conversation_id"], message_id))
        return db.execute("UPDATE messages SET content = %s WHERE id = %s RETURNING id, conversation_id, role, content, created_at", (content, message_id)).fetchone()


def delete_message(message_id: int, user_id: int) -> bool:
    with connection() as db:
        result = db.execute("DELETE FROM messages WHERE id = %s AND conversation_id IN (SELECT id FROM conversations WHERE user_id = %s)", (message_id, user_id))
        return result.rowcount > 0


def delete_conversation(conversation_id: int, user_id: int) -> bool:
    with connection() as db:
        result = db.execute("DELETE FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id))
        return result.rowcount > 0
