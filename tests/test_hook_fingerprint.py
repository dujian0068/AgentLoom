import functools
import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agentloom_runtime.hooks import Continue, HookDefinition, HookRegistry
from agentloom_runtime.hooks.fingerprint import handler_code_hash, legacy_handler_code_hash


async def file_handler(context, payload):
    return Continue()


def generated_handler(expression="1", *, filename="<generated-hook>"):
    source = (
        "async def hook(context, payload):\n"
        "    def nested():\n"
        f"        return {expression}\n"
        "    return nested()\n"
    )
    namespace = {}
    exec(compile(source, filename, "exec"), namespace)
    return namespace["hook"]


def test_file_source_hash_preserves_existing_publications():
    expected = (
        "sha256:" + hashlib.sha256(inspect.getsource(file_handler).encode("utf-8")).hexdigest()
    )
    assert handler_code_hash(file_handler) == expected
    assert legacy_handler_code_hash(file_handler) == expected
    registry = HookRegistry()
    registry.register(HookDefinition("source", "v1", file_handler))
    assert registry.resolve("source", "v1", expected_hash=expected).code_hash == expected


def test_generated_hash_preserves_constant_types_and_nested_code_changes():
    expressions = ["1", "True", "1.0", "'1'", "b'1'", "1j", "0.0", "-0.0", "(1, 2)"]
    fingerprints = [handler_code_hash(generated_handler(value)) for value in expressions]
    assert len(set(fingerprints)) == len(expressions)
    assert all(value.startswith("sha256:bytecode-v2:") for value in fingerprints)


def test_generated_hash_does_not_depend_on_deployment_filename():
    first = generated_handler("('value', 42)", filename="/worker-a/deploy/hooks.py")
    second = generated_handler("('value', 42)", filename="/worker-b/release/hooks.py")
    assert handler_code_hash(first) == handler_code_hash(second)


def test_generated_hash_is_stable_across_process_hash_seeds(tmp_path):
    runtime_path = Path(__file__).resolve().parents[1] / "packages" / "runtime"
    source = (
        "async def hook(context, payload):\n"
        "    def nested(value):\n"
        "        return value in {'alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta'}\n"
        "    return nested(payload)\n"
    )
    script = (
        "import json\n"
        "from agentloom_runtime.hooks.fingerprint import handler_code_hash, legacy_handler_code_hash\n"
        "namespace = {}\n"
        f"exec(compile({source!r}, '<dynamic-hook>', 'exec'), namespace)\n"
        "hook = namespace['hook']\n"
        "print(json.dumps({'current': handler_code_hash(hook), 'legacy': legacy_handler_code_hash(hook)}))\n"
    )
    results = []
    for seed in (1, 2, 3, 4):
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=tmp_path,
            env={
                "PATH": os.defpath,
                "PYTHONPATH": str(runtime_path),
                "PYTHONHASHSEED": str(seed),
                "PYTHON_DOTENV_DISABLED": "1",
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        results.append(json.loads(result.stdout))
    assert len({value["current"] for value in results}) == 1
    assert results[0]["current"].startswith("sha256:bytecode-v2:")
    # Demonstrate the original failure with actual separate interpreter seeds.
    assert len({value["legacy"] for value in results}) > 1


def test_legacy_generated_hash_is_only_accepted_by_explicit_compatibility_path():
    handler = generated_handler("('stable', 1)")
    current = handler_code_hash(handler)
    legacy = legacy_handler_code_hash(handler)
    assert current != legacy
    registry = HookRegistry()
    registry.register(HookDefinition("generated", "v1", handler))
    with pytest.raises(ValueError, match="code hash changed"):
        registry.resolve("generated", "v1", expected_hash=legacy)
    restored = registry.resolve("generated", "v1", expected_hash=legacy, allow_legacy_hash=True)
    assert restored.code_hash == legacy
    assert registry.resolve("generated", "v1").code_hash == current
    with pytest.raises(ValueError, match="code hash changed"):
        registry.resolve("generated", "v1", expected_hash="wrong", allow_legacy_hash=True)


def test_explicit_artifact_hash_is_respected_without_legacy_fallback():
    registry = HookRegistry()
    registry.register(HookDefinition("artifact", "v1", file_handler, code_hash="sha256:artifact"))
    definition = registry.resolve("artifact", "v1", expected_hash="sha256:artifact")
    assert definition.code_hash == "sha256:artifact"
    with pytest.raises(ValueError, match="code hash changed"):
        registry.resolve(
            "artifact",
            "v1",
            expected_hash=legacy_handler_code_hash(file_handler),
            allow_legacy_hash=True,
        )


def test_partial_handler_requires_explicit_artifact_hash():
    async def hook(context, payload, *, prefix):
        return Continue()

    handler = functools.partial(hook, prefix="deployment-config")
    assert inspect.iscoroutinefunction(handler)
    registry = HookRegistry()
    with pytest.raises(ValueError, match="explicit code_hash"):
        registry.register(HookDefinition("partial", "v1", handler))
    registry.register(HookDefinition("partial", "v1", handler, code_hash="sha256:configured"))
    definition = registry.resolve("partial", "v1", expected_hash="sha256:configured")
    assert definition.handler is handler
