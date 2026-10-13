"""Explicit schema initialization; application runtime never creates its own schema."""
from contextlib import contextmanager


class Database:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def connection(self):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.settings.database_url, row_factory=dict_row, connect_timeout=5,
                             options="-c statement_timeout=15000 -c lock_timeout=3000") as conn:
            yield conn

    def ready(self):
        with self.connection() as conn:
            row = conn.execute("SELECT signature FROM rag_schema WHERE id=1").fetchone()
            if not row or row["signature"] not in self.settings.readable_index_signatures:
                raise RuntimeError("Database index signature mismatch; migrate/reindex explicitly")

    def initialize(self):
        dimension = self.settings.embedding_dimension
        if not 1 <= dimension <= 2000:
            raise ValueError("Invalid embedding dimension")
        with self.connection() as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
            c.execute("CREATE TABLE IF NOT EXISTS rag_schema(id integer PRIMARY KEY CHECK(id=1), signature text NOT NULL)")
            old = c.execute("SELECT signature FROM rag_schema WHERE id=1 FOR UPDATE").fetchone()
            if old and old["signature"] not in self.settings.readable_index_signatures:
                raise RuntimeError("Existing database requires an explicit index migration")
            c.execute("""CREATE TABLE IF NOT EXISTS documents(
                id uuid PRIMARY KEY, tenant_id text NOT NULL, kb_id text NOT NULL,
                filename text NOT NULL, current_version uuid, deleted boolean NOT NULL DEFAULT false,
                created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""")
            c.execute("""CREATE TABLE IF NOT EXISTS versions(
                id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES documents(id),
                content_hash text NOT NULL, source_text text NOT NULL, signature text NOT NULL,
                state text NOT NULL CHECK(state IN ('pending','processing','ready','failed','superseded')),
                attempts integer NOT NULL DEFAULT 0, lease_token uuid, lease_until timestamptz,
                failure_code text, chunk_count integer, created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE(document_id, content_hash, signature))""")
            c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_pending_version ON versions(document_id)
                         WHERE state IN ('pending','processing')""")
            c.execute(f"""CREATE TABLE IF NOT EXISTS chunks(
                id uuid PRIMARY KEY, version_id uuid NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                ordinal integer NOT NULL, content text NOT NULL, embedding vector({dimension}) NOT NULL,
                UNIQUE(version_id, ordinal))""")
            c.execute("CREATE INDEX IF NOT EXISTS chunks_version ON chunks(version_id)")
            c.execute("CREATE INDEX IF NOT EXISTS docs_scope ON documents(tenant_id,kb_id) WHERE NOT deleted")
            c.execute("""CREATE TABLE IF NOT EXISTS upload_requests(
                tenant_id text NOT NULL, user_id text NOT NULL, idempotency_key text NOT NULL,
                payload_hash text NOT NULL, document_id uuid NOT NULL REFERENCES documents(id),
                version_id uuid NOT NULL REFERENCES versions(id),
                PRIMARY KEY(tenant_id,user_id,idempotency_key))""")
            c.execute("""CREATE TABLE IF NOT EXISTS conversations(
                id uuid PRIMARY KEY, tenant_id text NOT NULL, user_id text NOT NULL, kb_id text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now())""")
            c.execute("""CREATE TABLE IF NOT EXISTS chat_requests(
                id uuid PRIMARY KEY, tenant_id text NOT NULL, user_id text NOT NULL,
                idempotency_key text NOT NULL, conversation_id uuid NOT NULL REFERENCES conversations(id),
                payload_hash text NOT NULL, question text NOT NULL,
                state text NOT NULL CHECK(state IN ('running','complete','failed')),
                lease_token uuid NOT NULL, lease_until timestamptz NOT NULL,
                response jsonb, failure_code text, created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE(tenant_id,user_id,idempotency_key))""")
            c.execute("CREATE INDEX IF NOT EXISTS requests_conversation ON chat_requests(conversation_id,state)")
            c.execute("""CREATE TABLE IF NOT EXISTS messages(
                id bigserial PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES conversations(id),
                request_id uuid NOT NULL REFERENCES chat_requests(id), role text NOT NULL,
                content text NOT NULL, citations jsonb NOT NULL DEFAULT '[]', created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE(request_id,role))""")
            c.execute("""CREATE TABLE IF NOT EXISTS rate_limits(
                scope text NOT NULL, bucket bigint NOT NULL, count integer NOT NULL,
                PRIMARY KEY(scope,bucket))""")
            c.execute("INSERT INTO rag_schema(id,signature) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET signature=excluded.signature",
                      (self.settings.index_signature,))


if __name__ == "__main__":
    from .config import Settings
    Database(Settings.from_env()).initialize()
    print("RAG schema initialized. Existing legacy data was not imported.")
