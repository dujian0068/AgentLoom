"""Resolve published bindings against explicitly installed, trusted Python Hooks.

The deployment entry point may call ``register_trusted_hook`` before serving the
application. API requests select identifiers and configuration only; no request
can import a Python file, dependency, Git repository or uploaded extension.
"""

from copy import deepcopy

from agentloom_runtime.hooks import HookBinding, HookDefinition, HookManager, HookRegistry

from agentloom.schema import HookBindingInput

TRUSTED_HOOKS = HookRegistry()
_PUBLISHING = object()


def register_trusted_hook(definition: HookDefinition):
    """Explicit deployment registration, shared by all API workers via startup code."""
    return TRUSTED_HOOKS.register(definition)


def hook_manifest(manager):
    return {"module_id": manager.module_id, "config": manager.checkpoint_config()}


def hook_manager(config, saved_manifest=_PUBLISHING):
    """Resolve a draft, or verify an immutable publication when a manifest is given.

    Passing ``None`` explicitly denotes a legacy snapshot. It is accepted only
    without Hook bindings. A missing optional draft version resolves at publish
    time; execution uses that resolved version even if newer versions exist.
    """
    try:
        bindings = [
            HookBindingInput.model_validate(value).model_dump() for value in config.get("hooks", [])
        ]
    except (ValueError, TypeError):
        raise ValueError("Hook 绑定配置不合法") from None
    if saved_manifest is None and bindings:
        raise ValueError("已发布版本缺少 Hook 清单，请重新发布 Agent")
    if saved_manifest is not _PUBLISHING and saved_manifest is not None:
        if (
            not isinstance(saved_manifest, dict)
            or set(saved_manifest) != {"module_id", "config"}
            or saved_manifest["module_id"] != HookManager.module_id
            or not isinstance(saved_manifest["config"], dict)
            or not isinstance(saved_manifest["config"].get("bindings"), list)
        ):
            raise ValueError("已发布 Hook 清单格式不兼容，请重新发布 Agent")
        try:
            resolved = {
                value["binding_id"]: value["version"]
                for value in saved_manifest["config"]["bindings"]
            }
            for binding in bindings:
                if binding["version"] is None:
                    binding["version"] = resolved[binding["binding_id"]]
        except (KeyError, TypeError):
            raise ValueError("已发布 Hook 绑定与版本清单不一致") from None
    try:
        manager = HookManager(
            TRUSTED_HOOKS,
            [HookBinding(**value) for value in bindings],
            saved_config=saved_manifest["config"]
            if saved_manifest is not _PUBLISHING and saved_manifest is not None
            else {"total_timeout": 10.0, "bindings": []}
            if saved_manifest is None
            else None,
        )
    except (ValueError, TypeError):
        if saved_manifest is not _PUBLISHING and saved_manifest is not None:
            raise ValueError(
                "已发布 Hook 的版本、代码或配置已变化，请恢复原部署或重新发布 Agent"
            ) from None
        raise ValueError("Hook 绑定不可用，请检查部署注册的扩展版本、挂点和配置") from None
    if (
        saved_manifest is not _PUBLISHING
        and saved_manifest is not None
        and hook_manifest(manager) != saved_manifest
    ):
        raise ValueError("已发布 Hook 的版本、代码或配置已变化，请恢复原部署或重新发布 Agent")
    return manager


def published_hook_manifest(config):
    """Return detached JSON contracts; callable code never enters a published snapshot."""
    return deepcopy(hook_manifest(hook_manager(config)))
