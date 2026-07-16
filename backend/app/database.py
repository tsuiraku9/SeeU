from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(
    f"sqlite:///{settings.database_path.as_posix()}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_database() -> None:
    from . import models  # noqa: F401

    # Changing SQLite's journal mode is a database-wide write. Running this in
    # every connection callback lets concurrent first requests race on a fresh
    # process, which is especially unreliable on Docker Desktop bind mounts.
    # Lifespan calls this before the scheduler starts or the app accepts traffic.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
    Base.metadata.create_all(engine)
    _migrate_sqlite_columns()


def _migrate_sqlite_columns() -> None:
    """Apply additive SQLite migrations needed by existing self-hosted installs.

    The project deliberately keeps migrations additive and idempotent so an old
    app.db can be mounted into a newer container without a separate migration
    command. New tables are handled by ``create_all`` above.
    """

    additions = {
        "admins": {
            "session_version": "INTEGER NOT NULL DEFAULT 1",
        },
        "accounts": {
            "completeness_status": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
            "gap_detected_at": "DATETIME",
            "ledger_revision": "INTEGER NOT NULL DEFAULT 0",
        },
        "observed_content": {
            "retry_pending": "BOOLEAN NOT NULL DEFAULT 0",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "last_attempt_at": "DATETIME",
            "last_error": "TEXT",
        },
        "content_index": {
            "expected_media_count": "INTEGER NOT NULL DEFAULT 0",
            "verified_media_count": "INTEGER NOT NULL DEFAULT 0",
            "integrity_status": "VARCHAR(32) NOT NULL DEFAULT 'complete'",
        },
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {
                str(row[1])
                for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")').all()
            }
            for name, definition in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'
                    )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_observed_content_retry_pending "
            "ON observed_content (retry_pending)"
        )
