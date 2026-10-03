"""PostgreSQL fixture failures must not render connection credentials."""

from contextlib import contextmanager

import conftest
import psycopg
import pytest
from _pytest.runner import CallInfo
from psycopg_pool import PoolTimeout

FAKE_SECRET = "fake-db-secret-do-not-render"


def assert_safe_failure(request, operation, phase):
    call = CallInfo.from_call(operation, when="setup")
    assert call.excinfo is not None
    error = call.excinfo.value
    assert isinstance(error, pytest.fail.Exception)
    assert error.pytrace is False
    assert error.__context__ is None and error.__cause__ is None
    # Use pytest's actual failure renderer, including its --showlocals behavior.
    original_showlocals = request.config.option.showlocals
    try:
        request.config.option.showlocals = True
        rendered = str(request.node.repr_failure(call.excinfo))
    finally:
        request.config.option.showlocals = original_showlocals
    assert phase in rendered and "SSH 隧道" in rendered
    assert FAKE_SECRET not in rendered and "conninfo" not in rendered


@pytest.mark.parametrize("drop", [False, True])
@pytest.mark.parametrize("stage", ["connect", "execute", "exit"])
def test_schema_boundary_hides_setup_and_cleanup_connection_failures(
    monkeypatch, request, drop, stage
):
    @contextmanager
    def connect(**kwargs):
        conninfo = "password=" + kwargs["password"]
        if stage == "connect":
            raise psycopg.OperationalError(conninfo)

        class Connection:
            def execute(self, statement):
                if stage == "execute":
                    raise psycopg.OperationalError(conninfo)

        yield Connection()
        if stage == "exit":
            raise psycopg.OperationalError(conninfo)

    monkeypatch.setattr(conftest.psycopg, "connect", connect)
    assert_safe_failure(
        request,
        lambda: conftest._postgres_schema(
            "agentloom_test_fake", {"password": FAKE_SECRET}, drop=drop
        ),
        "清理临时 schema" if drop else "创建临时 schema",
    )


@pytest.mark.parametrize("stage", ["enter", "exit"])
def test_application_lifecycle_connection_errors_are_safe(monkeypatch, request, stage):
    class Client:
        def __init__(self, app):
            pass

        def __enter__(self):
            if stage == "enter":
                raise PoolTimeout(FAKE_SECRET)
            return self

        def __exit__(self, *args):
            if stage == "exit":
                raise psycopg.InterfaceError(FAKE_SECRET)

    monkeypatch.setattr(conftest, "TestClient", Client)

    def operation():
        with conftest._postgres_client():
            pass

    assert_safe_failure(
        request, operation, "初始化应用连接" if stage == "enter" else "关闭应用连接"
    )


def test_configuration_failures_do_not_expose_local_passwords(request):
    def operation():
        raise ValueError("invalid conninfo with " + FAKE_SECRET)

    assert_safe_failure(
        request,
        lambda: conftest._postgres_boundary("读取配置", operation, configuration=True),
        "读取配置",
    )


def test_pool_close_errors_use_safe_failure(request):
    def close():
        raise psycopg.OperationalError(FAKE_SECRET)

    assert_safe_failure(
        request, lambda: conftest._postgres_boundary("关闭连接池", close), "关闭连接池"
    )


@pytest.mark.parametrize(
    "error",
    [
        AssertionError("business assertion"),
        ValueError("business"),
        RuntimeError("programming error"),
    ],
)
def test_non_database_failures_are_not_reclassified(error):
    def operation():
        raise error

    with pytest.raises(type(error)) as raised:
        conftest._postgres_boundary("初始化应用连接", operation)
    assert raised.value is error


def test_client_body_failures_are_not_swallowed(monkeypatch):
    exited = []

    class Client:
        def __init__(self, app):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            exited.append(True)

    monkeypatch.setattr(conftest, "TestClient", Client)
    error = psycopg.OperationalError("business query failed")
    with pytest.raises(psycopg.OperationalError) as raised:
        with conftest._postgres_client():
            raise error
    assert raised.value is error and exited == [True]
