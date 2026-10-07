"""Composition root: select implementations once, then inject runtime ports."""

from copy import deepcopy

from .budget import normalized_profile
from .completion import EvidenceCompletionPolicy
from .context import BudgetCompactionPolicy, CharacterCompactionPolicy
from .context_manager import JournalContextManager
from .engine import Engine
from .execution_strategy import PlanStrategy, ReactStrategy
from .handlers import register_builtins
from .model_gateway import ProviderModelGateway
from .module_contracts import ExecutionLimits
from .modules import RuntimeModules
from .tool_runtime import ToolRegistry, ToolRuntime


def create_engine(
    snapshot,
    workspace,
    emit,
    decrypt,
    search,
    checkpoint=None,
    save=None,
    configure_tools=None,
    *,
    model_gateway=None,
    compaction_policy=None,
    completion_policy=None,
    strategy=None,
    limits=None,
    registry=None,
    context_manager=None,
):
    snapshot = deepcopy(snapshot)
    if "context_policy" in snapshot["config"]:
        snapshot["model_obj"].update(normalized_profile(snapshot["model_obj"]))
    if (
        checkpoint is not None
        and "modules" not in checkpoint
        and (registry is not None or configure_tools is not None)
    ):
        raise ValueError("旧检查点的自定义工具集合需要显式迁移")
    modules = RuntimeModules(
        model=model_gateway
        if model_gateway is not None
        else ProviderModelGateway(snapshot["model_obj"], decrypt),
        compaction=compaction_policy
        if compaction_policy is not None
        else (
            BudgetCompactionPolicy(snapshot["config"]["context_policy"], snapshot["model_obj"])
            if "context_policy" in snapshot["config"]
            else CharacterCompactionPolicy()
        ),
        completion=completion_policy
        if completion_policy is not None
        else EvidenceCompletionPolicy(),
        strategy=strategy
        if strategy is not None
        else (PlanStrategy() if snapshot["config"]["mode"] == "plan" else ReactStrategy()),
        limits=limits if limits is not None else ExecutionLimits(),
        context=context_manager if context_manager is not None else JournalContextManager(),
    )
    modules.bindings()  # Validate the durable contract before registering handlers.
    if registry is None:
        registry = ToolRegistry()
        with registry.implementation_namespace("builtin-tools/v1"):
            register_builtins(registry, snapshot, decrypt=decrypt, search=search)
    if configure_tools is not None:
        configure_tools(registry)
    tools = ToolRuntime(snapshot, registry=registry)
    return Engine(
        snapshot["config"],
        workspace,
        emit,
        checkpoint=deepcopy(checkpoint),
        save=save,
        tools=tools,
        modules=modules,
        model_id=snapshot["model_obj"]["model_id"],
    )
