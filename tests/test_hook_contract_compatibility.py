"""Recover trusted legacy Hooks without accepting contracts supplied by a checkpoint."""

import asyncio
import copy
from dataclasses import replace

import pytest
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookFailed,
    HookManager,
    HookPointRegistry,
    HookRegistry,
    PatchOutput,
    execute_operation,
)


async def observe(_ctx, _payload):
    return Continue()


async def changed_observer(_ctx, _payload):
    # A distinct implementation must not inherit an old publication's identity.
    result = Continue()
    return result


def catalog(handler=observe):
    registry = HookRegistry()
    registry.register(HookDefinition("observe", "v1", handler, replay_safe=True))
    return registry


def binding(**changes):
    return replace(
        HookBinding("audit", "observe", "tool.before", version="v1", config={"limit": 3}),
        **changes,
    )


def legacy_manager(registry=None, bindings=()):
    return HookManager(
        registry,
        bindings,
        points=HookPointRegistry(contract_version=1),
    )


def test_v1_after_failure_resumes_with_raw_result_and_completed_hooks_preserved():
    calls, checkpoints = [], []
    transient = {"fail": True}

    async def before(_ctx, _payload):
        calls.append("before")
        return Continue()

    async def redact(_ctx, _payload):
        calls.append("redact")
        return PatchOutput({"value": "redacted"})

    async def finish(_ctx, payload):
        calls.append("finish")
        assert payload["value"] == "redacted"
        if transient["fail"]:
            raise RuntimeError("temporary observer failure")
        return Continue()

    # The fixture's transient failure flag represents an execution failure, not a
    # changed deployment. All handlers are pure, explicitly replay-safe and keep
    # identical source code/registration across the simulated process restart.
    definitions = (
        HookDefinition("before", "v1", before, replay_safe=True),
        HookDefinition("redact", "v1", redact, replay_safe=True),
        HookDefinition("finish", "v1", finish, replay_safe=True),
    )
    bindings = [
        HookBinding("before", "before", "tool.before", version="v1"),
        HookBinding("redact", "redact", "tool.after", version="v1", priority=10),
        HookBinding("finish", "finish", "tool.after", version="v1", priority=20),
    ]

    def registered():
        registry = HookRegistry()
        for definition in definitions:
            registry.register(definition)
        return registry

    old = legacy_manager(registered(), bindings)
    published = old.checkpoint_config()
    assert "point_contract_version" not in published
    state = {}
    raw = {"value": "private", "status": "succeeded"}

    async def action(_payload):
        calls.append("action")
        return copy.deepcopy(raw)

    def execute(manager):
        return execute_operation(
            manager,
            "tool",
            {"arguments": {}},
            action,
            scope={"operation_id": "published-operation"},
            state=state,
            save=lambda: checkpoints.append(copy.deepcopy(state)),
        )

    with pytest.raises(HookFailed, match="finish"):
        asyncio.run(execute(old))
    assert any(item.get("raw_output") == raw for item in checkpoints)
    assert calls == ["before", "action", "redact", "finish"]
    assert state["actual_status"] == "succeeded" and state["stage"] == "after"
    assert state["after"]["cursor"] == 1

    # Resume from detached persisted data through the public saved_config entry.
    transient["fail"] = False
    state = copy.deepcopy(checkpoints[-1])
    restored = HookManager(registered(), bindings, saved_config=copy.deepcopy(published))
    assert restored.points.contract_version == 1
    assert restored.checkpoint_config() == published
    result = asyncio.run(execute(restored))
    assert result == {**raw, "value": "redacted"}
    assert calls == ["before", "action", "redact", "finish", "finish"]
    assert state["raw_output"] == raw and state["stage"] == "completed"
    assert asyncio.run(execute(restored)) == result
    assert calls == ["before", "action", "redact", "finish", "finish"]


def test_new_manager_keeps_v2_contract_in_publication_and_restoration():
    registry, bindings = catalog(), [binding()]
    current = HookManager(registry, bindings)
    config = current.checkpoint_config()
    assert config["point_contract_version"] == 2
    contract = config["bindings"][0]["point_contract"]["schema"]
    assert contract["additionalProperties"] is False
    assert set(contract["required"]) == {
        "name",
        "tool_id",
        "implementation_id",
        "parameters",
        "arguments",
    }
    restored = HookManager(registry, bindings, saved_config=config)
    assert restored.points.contract_version == 2
    assert restored.checkpoint_config() == config
    with pytest.raises(HookFailed, match="point schema"):
        asyncio.run(restored.run("tool.before", {}, scope={}, state={}, save=lambda: None))


@pytest.mark.parametrize("contract_version", [1, 2])
@pytest.mark.parametrize("forged_schema", [True, {}, {"type": "object", "required": ["secret"]}])
def test_saved_schema_is_compared_with_host_contract_never_installed(
    contract_version,
    forged_schema,
):
    registry, bindings = catalog(), [binding()]
    original = HookManager(
        registry, bindings, points=HookPointRegistry(contract_version=contract_version)
    ).checkpoint_config()
    tampered = copy.deepcopy(original)
    tampered["bindings"][0]["point_contract"]["schema"] = forged_schema
    with pytest.raises(ValueError, match="point contract changed"):
        HookManager(registry, bindings, saved_config=tampered)
    assert HookManager(registry, bindings, saved_config=original).checkpoint_config() == original


@pytest.mark.parametrize("contract_version", [1, 2])
@pytest.mark.parametrize("change", ["hash", "binding_config", "saved_config", "version", "code"])
def test_compatibility_does_not_accept_changed_hash_configuration_version_or_code(
    contract_version,
    change,
):
    registry, bindings = catalog(), [binding()]
    saved = HookManager(
        registry, bindings, points=HookPointRegistry(contract_version=contract_version)
    ).checkpoint_config()
    if change == "hash":
        saved["bindings"][0]["code_hash"] = "sha256:unverified"
    elif change == "binding_config":
        bindings = [binding(config={"limit": 9})]
    elif change == "saved_config":
        saved["bindings"][0]["config"]["limit"] = 9
    elif change == "version":
        registry.register(HookDefinition("observe", "v2", observe, replay_safe=True))
        bindings = [binding(version="v2")]
    else:
        registry = catalog(changed_observer)
    with pytest.raises(
        ValueError, match="(code hash changed|configuration or point contract changed)"
    ):
        HookManager(registry, bindings, saved_config=saved)


@pytest.mark.parametrize("change", ["add_version", "wrong_points", "unknown_version"])
def test_legacy_contract_version_cannot_be_silently_upgraded(change):
    registry, bindings = catalog(), [binding()]
    saved = legacy_manager(registry, bindings).checkpoint_config()
    kwargs = {}
    if change == "add_version":
        saved["point_contract_version"] = 2
    elif change == "wrong_points":
        kwargs["points"] = HookPointRegistry(contract_version=2)
    else:
        saved["point_contract_version"] = 99
    with pytest.raises(ValueError):
        HookManager(registry, bindings, saved_config=saved, **kwargs)


def test_old_empty_configuration_and_completed_operation_still_restore():
    old_config = {"total_timeout": 10.0, "bindings": []}
    old = legacy_manager()
    assert old.checkpoint_config() == old_config
    calls, state = [], {}

    async def action(_payload):
        calls.append("action")
        return {"value": "legacy result"}

    def execute(manager):
        return execute_operation(
            manager,
            "tool",
            {},
            action,
            scope={},
            state=state,
            save=lambda: None,
        )

    assert asyncio.run(execute(old)) == {"value": "legacy result"}
    state = copy.deepcopy(state)
    restored = HookManager(saved_config=copy.deepcopy(old_config))
    assert restored.points.contract_version == 1
    assert restored.checkpoint_config() == old_config
    assert asyncio.run(execute(restored)) == {"value": "legacy result"}
    assert calls == ["action"]


def test_old_empty_publication_cannot_gain_new_hooks_during_recovery():
    with pytest.raises(ValueError, match="configuration or point contract changed"):
        HookManager(catalog(), [binding()], saved_config={"total_timeout": 10.0, "bindings": []})


@pytest.mark.parametrize("contract_version", [1, 2])
def test_restoration_pins_an_originally_unspecified_version(contract_version):
    registry = catalog()
    original_bindings = [binding(version=None)]
    saved = HookManager(
        registry, original_bindings, points=HookPointRegistry(contract_version=contract_version)
    ).checkpoint_config()
    registry.register(HookDefinition("observe", "v2", changed_observer, replay_safe=True))
    restored = HookManager(registry, original_bindings, saved_config=saved)
    assert restored.checkpoint_config() == saved
    assert restored.checkpoint_config()["bindings"][0]["version"] == "v1"


def test_old_dynamic_fingerprint_restores_original_chain_without_repeating_action():
    from agentloom_runtime.hooks.fingerprint import legacy_handler_code_hash

    calls, checkpoints = [], []
    failure = {"enabled": True}

    def dynamic_handler():
        namespace = {"Continue": Continue, "calls": calls, "failure": failure}
        exec(
            "async def handler(ctx, payload):\n"
            "    calls.append('hook')\n"
            "    if payload['value'] in {'alpha', 'beta', 'gamma', 'delta'}:\n"
            "        if failure['enabled']:\n"
            "            raise RuntimeError('transient failure')\n"
            "    return Continue()\n",
            namespace,
        )
        return namespace["handler"]

    # Model a real pre-upgrade publication: source-less code used the old
    # repr-based hash, without a typed-bytecode prefix or contract version field.
    handler = dynamic_handler()
    old_hash = legacy_handler_code_hash(handler)
    old_registry = HookRegistry()
    old_registry.register(
        HookDefinition("dynamic", "v1", handler, replay_safe=True, code_hash=old_hash)
    )
    bindings = [HookBinding("dynamic", "dynamic", "tool.after", version="v1")]
    old_manager = legacy_manager(old_registry, bindings)
    published = old_manager.checkpoint_config()
    state = {}

    async def action(_payload):
        calls.append("action")
        return {"value": "alpha"}

    def execute(manager):
        return execute_operation(
            manager,
            "tool",
            {},
            action,
            scope={"operation_id": "dynamic-operation"},
            state=state,
            save=lambda: checkpoints.append(copy.deepcopy(state)),
        )

    with pytest.raises(HookFailed, match="dynamic"):
        asyncio.run(execute(old_manager))
    assert calls == ["action", "hook"]
    assert state["raw_output"] == {"value": "alpha"}
    saved_chain = state["chain"]

    # A fresh registration now computes canonical bytecode-v2, but restoration
    # can verify the exact legacy fingerprint against these same code bytes.
    registry = HookRegistry()
    registry.register(HookDefinition("dynamic", "v1", dynamic_handler(), replay_safe=True))
    canonical_hash = registry.resolve("dynamic", "v1").code_hash
    assert canonical_hash.startswith("sha256:bytecode-v2:")
    assert canonical_hash != old_hash
    forged = copy.deepcopy(published)
    forged["bindings"][0]["code_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="code hash changed"):
        HookManager(registry, bindings, saved_config=forged)

    restored = HookManager(registry, bindings, saved_config=copy.deepcopy(published))
    assert restored.checkpoint_config() == published
    failure["enabled"] = False
    state = copy.deepcopy(checkpoints[-1])
    assert asyncio.run(execute(restored)) == {"value": "alpha"}
    assert state["chain"] == saved_chain
    assert calls == ["action", "hook", "hook"]
    assert asyncio.run(execute(restored)) == {"value": "alpha"}
    assert calls == ["action", "hook", "hook"]
