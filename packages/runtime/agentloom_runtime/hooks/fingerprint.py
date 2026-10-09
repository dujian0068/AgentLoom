"""Code fingerprints; deployment dependencies need an explicit artifact digest."""

import hashlib
import inspect
import json
import sys
import types


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _constant(value):
    kind = type(value)
    if value is None:
        return ["none"]
    if value is Ellipsis:
        return ["ellipsis"]
    if kind is bool:
        return ["bool", value]
    if kind is int:
        return ["int", str(value)]
    if kind is float:
        return ["float", value.hex()]
    if kind is complex:
        return ["complex", value.real.hex(), value.imag.hex()]
    if kind is str:
        return ["str", value]
    if kind is bytes:
        return ["bytes", value.hex()]
    if kind is tuple:
        return ["tuple", [_constant(item) for item in value]]
    if kind is frozenset:
        return ["frozenset", sorted((_constant(item) for item in value), key=_encoded)]
    if kind is types.CodeType:
        return ["code", _code(value)]
    raise ValueError("Hook code contains an unsupported constant; provide an explicit code_hash")


def _code(code):
    return {
        "bytecode": code.co_code.hex(),
        "constants": [_constant(value) for value in code.co_consts],
        "names": code.co_names,
        "variables": code.co_varnames,
        "freevars": code.co_freevars,
        "cellvars": code.co_cellvars,
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
        "exceptiontable": getattr(code, "co_exceptiontable", b"").hex(),
    }


def handler_code_hash(handler):
    """Preserve existing source hashes; canonicalize source-less code separately.

    A function hash does not include closure values, instance state, imported
    dependencies or global configuration. Use HookDefinition.code_hash for a
    deployment artifact covering those inputs; mutable execution state is not code.
    """
    try:
        source = inspect.getsource(handler).encode("utf-8")
    except (OSError, TypeError):
        code = getattr(handler, "__code__", None)
        if not isinstance(code, types.CodeType):
            raise ValueError("Hook callable requires an explicit code_hash") from None
        payload = {"python": sys.implementation.cache_tag, "code": _code(code)}
        value = hashlib.sha256(_encoded(payload).encode("utf-8")).hexdigest()
        return "sha256:bytecode-v2:" + value
    return "sha256:" + hashlib.sha256(source).hexdigest()


def legacy_handler_code_hash(handler):
    """Verify an old fingerprint only; never generate new publications with it."""
    try:
        source = inspect.getsource(handler).encode("utf-8")
    except (OSError, TypeError):
        code = getattr(handler, "__code__", None)
        if not isinstance(code, types.CodeType):
            return None

        def legacy_data(value):
            return {
                "bytecode": value.co_code.hex(),
                "names": value.co_names,
                "variables": value.co_varnames,
                "constants": [
                    legacy_data(item) if isinstance(item, types.CodeType) else repr(item)
                    for item in value.co_consts
                ],
            }

        source = json.dumps(legacy_data(code), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(source).hexdigest()
