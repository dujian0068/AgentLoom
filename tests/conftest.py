import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
sys.path.insert(0, str(ROOT / "packages/runtime"))
# Ordinary tests must never use the database configured in the user's .env.
os.environ["DB_BACKEND"] = "sqlite"
import psycopg
import pytest
from agentloom import database
from agentloom import store as db
from agentloom.app import app
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg_pool import PoolTimeout


def pytest_addoption(parser):
    parser.addoption(
        "--postgres",
        action="store_true",
        help="Run integration cases in temporary PostgreSQL schemas",
    )


def _postgres_boundary(phase, operation, *, configuration=False):
    """Do not let infrastructure tracebacks expose psycopg connection kwargs."""
    __tracebackhide__ = True
    try:
        return operation()
    except (psycopg.Error, PoolTimeout):
        pass
    except ValueError:
        if not configuration:
            raise
    # Raise outside except so even verbose reports have no original exception chain.
    pytest.fail(
        f"PostgreSQL 测试{phase}失败；请检查 SSH 隧道、数据库配置和测试库权限。"
        "连接参数及底层异常已隐藏。",
        pytrace=False,
    )


def _postgres_schema(schema, settings, *, drop=False):
    def execute():
        with psycopg.connect(**settings) as connection:
            statement = "DROP SCHEMA {} CASCADE" if drop else "CREATE SCHEMA {}"
            connection.execute(sql.SQL(statement).format(sql.Identifier(schema)))

    _postgres_boundary("清理临时 schema" if drop else "创建临时 schema", execute)


@contextmanager
def _postgres_client():
    manager = TestClient(app)
    client = _postgres_boundary("初始化应用连接", manager.__enter__)
    try:
        yield client
    finally:
        # Keep the guard around lifecycle operations, never around test execution.
        _postgres_boundary("关闭应用连接", lambda: manager.__exit__(None, None, None))


@pytest.fixture
def client(tmp_path, monkeypatch, request):
    from agentloom.app import AUTH_ATTEMPTS
    from agentloom.state import PENDING_FINALIZATIONS

    AUTH_ATTEMPTS.clear()
    PENDING_FINALIZATIONS.clear()
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setattr(db, "DB", tmp_path / "test.db")
    schema = None
    admin_settings = None
    postgres = request.config.getoption("--postgres")
    if postgres:
        _postgres_boundary("关闭先前连接", db.close)
        admin_settings = _postgres_boundary(
            "读取配置", database.postgres_settings, configuration=True
        )
        candidate = "agentloom_test_" + uuid.uuid4().hex
        _postgres_schema(candidate, admin_settings)
        schema = candidate
        monkeypatch.setenv("DB_BACKEND", "postgresql")
        monkeypatch.setenv("DB_SCHEMA", schema)
    try:
        with _postgres_client() if postgres else TestClient(app) as client:
            r = client.post(
                "/api/auth/setup",
                json={
                    "email": "tester@example.test",
                    "name": "测试管理员",
                    "password": "local-test-password",
                },
            )
            assert r.status_code == 200
            yield client

    finally:
        PENDING_FINALIZATIONS.clear()
        try:
            if postgres:
                _postgres_boundary("关闭连接池", db.close)
            else:
                db.close()
        finally:
            if schema:
                # Delete only the unique schema this fixture created, never public/user data.
                _postgres_schema(schema, admin_settings, drop=True)


@pytest.fixture
def model(client):
    r = client.post(
        "/api/models",
        json={
            "name": "Test Model",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "model_id": "test-model",
            "api_key": "unit-test-secret",
            "purpose": "chat",
        },
    )
    assert r.status_code == 200
    assert "unit-test-secret" not in r.text
    return r.json()
