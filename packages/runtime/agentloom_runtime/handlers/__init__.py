"""Register runtime capabilities independently of the model loop."""

from . import delegation, filesystem, knowledge, mcp, planning, skills, workspace


def register_builtins(registry, snapshot, *, decrypt, search, workspace_provider=None):
    if workspace_provider is None:
        workspace.register(registry, snapshot, decrypt=decrypt, search=search)
    else:
        filesystem.register(registry, snapshot, workspace_provider)
    skills.register(
        registry,
        snapshot,
        decrypt=decrypt,
        search=search,
        include_command=workspace_provider is None,
    )
    for module in (planning, knowledge, delegation, mcp):
        module.register(registry, snapshot, decrypt=decrypt, search=search)
