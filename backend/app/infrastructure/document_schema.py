from ..core.config import DOCUMENT_EMBEDDING_DIMENSIONS


def execute_schema_statements(database_connection, statements: tuple[str, ...]) -> None:
    for statement in statements:
        database_connection.execute(statement)


def create_document_tables(database_connection, embedding_dimensions: int) -> None:
    table_statements = (
        """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'application/pdf',
            storage_name TEXT NOT NULL UNIQUE,
            checksum_sha256 TEXT NOT NULL,
            analysis_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'ready', 'failed', 'cancelled')),
            analysis_mode TEXT CHECK (analysis_mode IN ('basic', 'deep')),
            progress_percent INTEGER NOT NULL DEFAULT 0
                CHECK (progress_percent BETWEEN 0 AND 100),
            page_count INTEGER,
            extracted_token_count INTEGER,
            summary TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, checksum_sha256, analysis_version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_documents (
            conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            attached_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (conversation_id, document_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS message_documents (
            message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            PRIMARY KEY (message_id, document_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS document_pages (
            id BIGSERIAL PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_number INTEGER NOT NULL,
            narrative_text TEXT NOT NULL DEFAULT '',
            token_count INTEGER NOT NULL,
            headings JSONB NOT NULL DEFAULT '[]'::jsonb,
            extraction_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
            UNIQUE (document_id, page_number)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS document_tables (
            id BIGSERIAL PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_number INTEGER NOT NULL,
            table_number INTEGER NOT NULL,
            rows JSONB NOT NULL,
            markdown TEXT NOT NULL,
            token_count INTEGER NOT NULL,
            UNIQUE (document_id, page_number, table_number)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS document_nodes (
            id BIGSERIAL PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            parent_id BIGINT REFERENCES document_nodes(id) ON DELETE SET NULL,
            node_type TEXT NOT NULL
                CHECK (node_type IN ('evidence', 'table', 'packet', 'section', 'major', 'overview', 'small', 'medium', 'large', 'extralarge', 'root')),
            hierarchy_level INTEGER NOT NULL,
            source_table_id BIGINT REFERENCES document_tables(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            page_start INTEGER NOT NULL,
            page_end INTEGER NOT NULL,
            token_count INTEGER NOT NULL,
            embedding vector({embedding_dimensions}),
            search_vector TSVECTOR GENERATED ALWAYS AS (
                to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
            ) STORED,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (page_start <= page_end)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS document_node_sources (
            summary_node_id BIGINT NOT NULL REFERENCES document_nodes(id) ON DELETE CASCADE,
            source_node_id BIGINT NOT NULL REFERENCES document_nodes(id) ON DELETE CASCADE,
            PRIMARY KEY (summary_node_id, source_node_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS document_analysis_jobs (
            id BIGSERIAL PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'retrying')),
            stage TEXT NOT NULL DEFAULT 'queued',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            claimed_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS answer_jobs (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            assistant_message_id BIGINT REFERENCES messages(id) ON DELETE SET NULL,
            question TEXT NOT NULL,
            answer_depth TEXT CHECK (answer_depth IN ('overview', 'section', 'focused', 'evidence')),
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'waiting_for_documents', 'running', 'completed', 'failed', 'cancelled', 'retrying')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            claimed_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS answer_job_documents (
            answer_job_id BIGINT NOT NULL REFERENCES answer_jobs(id) ON DELETE CASCADE,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            PRIMARY KEY (answer_job_id, document_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS answer_evidence (
            assistant_message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            source_id BIGINT NOT NULL REFERENCES document_nodes(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            PRIMARY KEY (assistant_message_id, source_id)
        )
        """,
    )
    execute_schema_statements(database_connection, table_statements)


def apply_document_schema_migrations(database_connection) -> None:
    migration_statements = ("DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'answer_evidence' AND column_name = 'document_node_id') AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'answer_evidence' AND column_name = 'source_id') THEN ALTER TABLE answer_evidence RENAME COLUMN document_node_id TO source_id; END IF; END $$", "ALTER TABLE document_nodes ADD COLUMN IF NOT EXISTS source_table_id BIGINT REFERENCES document_tables(id) ON DELETE CASCADE", "ALTER TABLE document_nodes DROP CONSTRAINT IF EXISTS document_nodes_node_type_check", "ALTER TABLE document_nodes ADD CONSTRAINT document_nodes_node_type_check CHECK (node_type IN ('evidence', 'table', 'packet', 'section', 'major', 'overview', 'small', 'medium', 'large', 'extralarge', 'root'))", "ALTER TABLE answer_jobs ADD COLUMN IF NOT EXISTS answer_depth TEXT CHECK (answer_depth IN ('overview', 'section', 'focused', 'evidence'))", "DROP INDEX IF EXISTS document_summary_nodes_scope_unique")
    execute_schema_statements(database_connection, migration_statements)


def create_document_indexes(database_connection) -> None:
    index_statements = (
        "CREATE INDEX IF NOT EXISTS documents_owner_status_index ON documents (user_id, status, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS conversation_documents_document_index ON conversation_documents (document_id)",
        "CREATE INDEX IF NOT EXISTS document_pages_lookup_index ON document_pages (document_id, page_number)",
        "CREATE INDEX IF NOT EXISTS document_tables_lookup_index ON document_tables (document_id, page_number)",
        "CREATE INDEX IF NOT EXISTS document_nodes_scope_index ON document_nodes (document_id, node_type, page_start, page_end)",
        "CREATE INDEX IF NOT EXISTS document_nodes_source_table_index ON document_nodes (source_table_id) WHERE source_table_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS document_nodes_search_index ON document_nodes USING GIN (search_vector)",
        "CREATE INDEX IF NOT EXISTS document_nodes_embedding_index ON document_nodes USING hnsw (embedding vector_cosine_ops)",
        "CREATE INDEX IF NOT EXISTS document_analysis_jobs_ready_index ON document_analysis_jobs (status, available_at, id)",
        "CREATE INDEX IF NOT EXISTS answer_jobs_ready_index ON answer_jobs (status, available_at, id)",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS answer_jobs_one_unresolved_per_conversation
        ON answer_jobs (conversation_id)
        WHERE status IN ('queued', 'waiting_for_documents', 'running', 'retrying')
        """,
    )
    execute_schema_statements(database_connection, index_statements)


def initialize_document_schema(database_connection) -> None:
    embedding_dimensions = int(DOCUMENT_EMBEDDING_DIMENSIONS)
    database_connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    create_document_tables(database_connection, embedding_dimensions)
    apply_document_schema_migrations(database_connection)
    create_document_indexes(database_connection)
