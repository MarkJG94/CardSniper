"""Engine / session setup."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(self, path: Path | str):
        url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{path}"
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if str(path) == ":memory:":
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **kwargs)
        event.listen(self.engine, "connect", _sqlite_pragmas)
        Base.metadata.create_all(self.engine)
        add_missing_columns(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def add_missing_columns(engine: Engine) -> list[str]:
    """Tiny migration: add columns (and indexes) that newer versions of the models define."""
    added = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                                      f"{col.type.compile(engine.dialect)}"))
                    added.append(f"{table.name}.{col.name}")
            for index in table.indexes:
                index.create(conn, checkfirst=True)
    return added


def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
