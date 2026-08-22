"""Shared offline defaults and compatibility for the unified pytest entry."""

from __future__ import annotations

import asyncio
import inspect
import os

import pytest


os.environ.setdefault("TG_BOT_TOKEN", "test_token")
os.environ.setdefault("DEEPSEEK_API_KEY", "test_key")
os.environ.setdefault("ALIYUN_DASHSCOPE_API_KEY", "test_key")
os.environ.setdefault("DEEPSEEK_BASE_URL", "https://api.test.invalid/v1")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("DATABASE_PATH", "/tmp/my-telegram-bot-pytest.db")


@pytest.fixture(autouse=True)
def isolated_runtime_state(tmp_path):
    """Give every legacy test a DB and singleton state of its own.

    The old files were originally run one-by-one and each changed DATABASE_PATH
    during import. A single pytest process exposes that global-state coupling;
    this fixture restores the isolation those scripts previously relied on.
    """

    from agent.core.event_bus import EventBus
    from agent.pipeline.phases.before_turn import _sessions
    from config.settings import settings
    import persistence.database as database

    db_path = str(tmp_path / "memory.db")
    os.environ["DATABASE_PATH"] = db_path
    settings.DATABASE_PATH = db_path
    old_local = database._local
    if old_local is not None:
        connection = getattr(old_local, "conn", None)
        if connection is not None:
            connection.close()
    database._local = None
    _sessions.clear()
    EventBus._instance = None

    yield

    current_local = database._local
    if current_local is not None:
        connection = getattr(current_local, "conn", None)
        if connection is not None:
            connection.close()
    database._local = None
    _sessions.clear()
    EventBus._instance = None


def pytest_pyfunc_call(pyfuncitem):
    """Run legacy async test functions when pytest-asyncio is unavailable.

    CI installs pytest-asyncio. This fallback keeps old script-style tests
    executable in constrained development environments without rewriting them.
    """

    if pyfuncitem.config.pluginmanager.hasplugin("asyncio"):
        return None
    test_function = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_function):
        return None
    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in inspect.signature(test_function).parameters
    }
    asyncio.run(test_function(**kwargs))
    return True
