from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def enable_sqlite_foreign_keys(sqlalchemy_engine: Engine) -> None:
    if not sqlalchemy_engine.url.get_backend_name().startswith("sqlite"):
        return

    @event.listens_for(sqlalchemy_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


enable_sqlite_foreign_keys(engine)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility(engine)


def ensure_schema_compatibility(sqlalchemy_engine: Engine) -> None:
    if not sqlalchemy_engine.url.get_backend_name().startswith("sqlite"):
        return

    inspector = inspect(sqlalchemy_engine)
    if "channel_sync_links" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("channel_sync_links")}
    if "newapi_groups" not in columns:
        with sqlalchemy_engine.begin() as connection:
            connection.execute(text("ALTER TABLE channel_sync_links ADD COLUMN newapi_groups TEXT NOT NULL DEFAULT 'default'"))
