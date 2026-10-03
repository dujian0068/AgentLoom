"""Database configuration and a small DB-API adapter for SQLite/PostgreSQL."""

import hashlib
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env", override=False, interpolate=False)
IntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError)
_pool = None
_pool_guard = threading.Lock()


def backend():
    value = os.getenv("DB_BACKEND", "postgresql" if os.getenv("DB_HOST") else "sqlite").lower()
    if value not in ("sqlite", "postgresql"):
        raise ValueError("DB_BACKEND 必须是 sqlite 或 postgresql")
    return value


def postgres_settings():
    required = {key: os.getenv(key, "") for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")}
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError("PostgreSQL 配置缺少：" + ", ".join(missing) + "，请填写项目 .env")
    try:
        port = int(os.getenv("DB_PORT", "5432"))
        timeout = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))
    except ValueError:
        raise ValueError("DB_PORT 和 DB_CONNECT_TIMEOUT 必须是整数") from None
    if not 1 <= port <= 65535 or not 1 <= timeout <= 60:
        raise ValueError("DB_PORT 或 DB_CONNECT_TIMEOUT 超出允许范围")
    sslmode = os.getenv("DB_SSLMODE", "prefer")
    if sslmode not in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full"):
        raise ValueError("DB_SSLMODE 不合法")
    schema = os.getenv("DB_SCHEMA", "public")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("DB_SCHEMA 仅允许小写字母、数字和下划线")
    # Connection kwargs keep passwords out of URLs and handle special characters literally.
    return {
        "host": required["DB_HOST"],
        "port": port,
        "dbname": required["DB_NAME"],
        "user": required["DB_USER"],
        "password": required["DB_PASSWORD"],
        "sslmode": sslmode,
        "connect_timeout": timeout,
        "application_name": "agentloom",
        "options": f"-c search_path={schema} -c statement_timeout=30000 -c lock_timeout=30000",
    }


def pool():
    global _pool
    with _pool_guard:
        if _pool is None:
            _pool = ConnectionPool(
                kwargs={**postgres_settings(), "autocommit": True},
                min_size=1,
                max_size=8,
                timeout=10,
                max_waiting=32,
                open=True,
                name="agentloom",
            )
        return _pool


def close():
    global _pool
    with _pool_guard:
        current, _pool = _pool, None
    if current is not None:
        current.close()


def pg_parameters(sql):
    """Translate our qmark SQL while leaving quoted literals/identifiers untouched."""
    result = []
    quote = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        # psycopg interprets percent signs whenever bound parameters are supplied.
        if ch == "%":
            result.append("%%")
        elif quote:
            result.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    result.append(quote)
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            result.append(ch)
        elif ch == "?":
            result.append("%s")
        else:
            result.append(ch)
        i += 1
    return "".join(result)


class Row(dict):
    """Mapping rows also support the scalar-query convention row[0]."""

    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.names = [column[0] for column in cursor.description] if cursor.description else []

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        values = self.cursor.fetchone()
        return Row(zip(self.names, values)) if values is not None else None

    def fetchall(self):
        return [Row(zip(self.names, values)) for values in self.cursor.fetchall()]

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class Connection:
    def __init__(self, raw, dialect):
        self.raw = raw
        self.dialect = dialect

    def execute(self, sql, args=()):
        if self.dialect == "postgresql" and args:
            sql = pg_parameters(sql)
        cursor = self.raw.execute(sql, args) if args else self.raw.execute(sql)
        return Cursor(cursor)


@contextmanager
def connect(path, transactional=True):
    dialect = backend()
    if dialect == "postgresql":
        # A lost connection is discarded by the pool. Writes are never replayed.
        with pool().connection() as raw:
            if transactional:
                with raw.transaction():
                    yield Connection(raw, dialect)
            else:
                yield Connection(raw, dialect)
    else:
        raw = sqlite3.connect(path, timeout=30)
        raw.execute("PRAGMA foreign_keys=ON")
        try:
            with raw:
                yield Connection(raw, dialect)
        finally:
            raw.close()


def lock(connection, scope):
    if connection.dialect == "postgresql":
        # Stable across processes and starts; Python hash() is deliberately randomized.
        key = int.from_bytes(hashlib.blake2b(scope.encode(), digest_size=8).digest(), signed=True)
        connection.execute("SELECT pg_advisory_xact_lock(?)", (key,))


@contextmanager
def transaction(path, scope):
    with connect(path) as connection:
        if connection.dialect == "sqlite":
            connection.execute("BEGIN IMMEDIATE")
        else:
            lock(connection, scope)
        yield connection
