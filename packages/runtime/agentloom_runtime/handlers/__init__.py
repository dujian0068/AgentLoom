"""Register runtime capabilities independently of the model loop."""

from . import delegation, knowledge, mcp, planning, skills, workspace


def register_builtins(registry, snapshot, *, decrypt, search):
    for module in (workspace, planning, skills, knowledge, delegation, mcp):
        module.register(registry, snapshot, decrypt=decrypt, search=search)
