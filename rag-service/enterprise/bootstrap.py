"""Run only as a migration job with admin credentials, never by API/worker."""
from dataclasses import replace
import os
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .db import Database


def main():
    from psycopg import sql
    settings = Settings.from_env()
    admin_url = Path(os.environ["DATABASE_ADMIN_URL_FILE"]).read_text().strip()
    password = Path(os.environ["DATABASE_APP_PASSWORD_FILE"]).read_text().strip()
    if len(password) < 32 or urlparse(settings.database_url).username != "rag_app":
        raise RuntimeError("Bootstrap requires a long application password and rag_app runtime identity")
    database = Database(replace(settings, database_url=admin_url))
    database.initialize()
    with database.connection() as c:
        exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname='rag_app'").fetchone()
        statement = "ALTER ROLE rag_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}" if exists else "CREATE ROLE rag_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
        c.execute(sql.SQL(statement).format(sql.Literal(password)))
        c.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        c.execute("GRANT USAGE ON SCHEMA public TO rag_app")
        c.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO rag_app")
        c.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app")
    Database(settings).ready()
    print("Schema initialized and runtime credentials verified.")


if __name__ == "__main__":
    main()
