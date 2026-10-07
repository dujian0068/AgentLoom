"""Default model port; provider protocol and secrets stay outside the Loop."""

from copy import deepcopy

from . import provider
from .module_contracts import ModelRequest


class ProviderModelGateway:
    module_id = "chat-completions/v1"

    def __init__(self, model, resolve_secret):
        self._model = deepcopy(model)
        self._resolve_secret = resolve_secret

    def checkpoint_config(self):
        return {key: self._model.get(key) for key in ("provider", "base_url", "model_id")}

    async def invoke(self, request: ModelRequest) -> dict:
        return await provider.chat(
            self._model,
            request.messages,
            request.tools,
            self._resolve_secret(self._model.get("secret", "")),
        )
