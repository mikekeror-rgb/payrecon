"""Small DB-API wrapper so the adapters run on SQLite (tests, single machine) and PostgreSQL (psycopg 3 or 2).

It does five things (the fifth: results are fetched under the lock, see Result): converts `?` placeholders for Postgres, binds Decimal/datetime the way each engine
wants, gives one re-entrant transaction context, and returns plain tuples. Nothing here is an ORM.
Postgres support is written to the standard DB-API but has not been run against a live server from Python
in this repo's tests (see README, Limits).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterator, Sequence


class Result:
    """Rows fetched while the connection lock was still held. A live DB-API cursor is not safe to hand out: another
    thread's COMMIT or ROLLBACK on the shared connection can reset it, and the caller's fetchone() then returns
    None for a row that exists (seen under load as a callback that vanished between two statements)."""

    __slots__ = ("_rows", "_i", "rowcount")

    def __init__(self, rows: list[tuple], rowcount: int):
        self._rows, self._i, self.rowcount = rows, 0, rowcount

    def fetchone(self) -> tuple | None:
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self) -> list[tuple]:
        rest, self._i = self._rows[self._i:], len(self._rows)
        return rest


def to_dec(v: Any) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def to_dt(v: Any) -> datetime:
    dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, conn: Any, dialect: str):
        if dialect not in ("sqlite", "postgres"):
            raise ValueError(dialect)
        self.conn, self.dialect = conn, dialect
        self._lock = threading.RLock()
        self._depth = 0

    @classmethod
    def sqlite(cls, path: str = ":memory:") -> "Database":
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return cls(conn, "sqlite")

    @classmethod
    def postgres(cls, dsn: str) -> "Database":
        try:
            import psycopg  # type: ignore

            return cls(psycopg.connect(dsn, autocommit=True), "postgres")
        except ImportError:
            pass
        try:
            import psycopg2  # type: ignore

            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            return cls(conn, "postgres")
        except ImportError as e:
            raise RuntimeError("install a Postgres driver: pip install 'psycopg[binary]'") from e

    @classmethod
    def from_url(cls, url: str) -> "Database":
        if url.startswith(("postgres://", "postgresql://")):
            return cls.postgres(url)
        return cls.sqlite(url.removeprefix("sqlite:///") if url.startswith("sqlite:///") else url)

    # ---- binding ---------------------------------------------------------------

    def _bind(self, v: Any) -> Any:
        if isinstance(v, Enum):
            return v.value
        if self.dialect == "sqlite":
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, datetime):
                return v.astimezone(timezone.utc).isoformat(timespec="microseconds") if v.tzinfo else v.isoformat()
        return v

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    # ---- execution -------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute(self._sql(sql), tuple(self._bind(p) for p in params))
                rows = [tuple(r) for r in cur.fetchall()] if cur.description else []
                return Result(rows, cur.rowcount)
            finally:
                cur.close()

    def script(self, ddl: str) -> None:
        with self._lock:
            if self.dialect == "sqlite":
                self.conn.executescript(ddl)
            else:
                self.conn.cursor().execute(ddl)

    @contextmanager
    def tx(self) -> Iterator[None]:
        """One transaction; nested use joins the outer one. A BaseException (including a simulated crash)
        rolls back, so nothing is ever half-written."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.execute("BEGIN IMMEDIATE" if self.dialect == "sqlite" else "BEGIN")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                self.execute("ROLLBACK")
                raise
            else:
                self._depth = 0
                self.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()
