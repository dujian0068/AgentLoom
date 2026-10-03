"""Composition root: register tool implementations outside the agent loop."""

from .engine import Engine
from .handlers import register_builtins
from .tool_runtime import ToolRegistry, ToolRuntime


def create_engine(
    snapshot, workspace, emit, decrypt, search, checkpoint=None, save=None, configure_tools=None
):
    registry = ToolRegistry()
    register_builtins(registry, snapshot, decrypt=decrypt, search=search)
    if configure_tools is not None:
        configure_tools(registry)
    tools = ToolRuntime(snapshot, registry=registry)
    return Engine(snapshot, workspace, emit, decrypt, checkpoint=checkpoint, save=save, tools=tools)
