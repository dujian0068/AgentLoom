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
        model = deepcopy(self._model)
        for name in ("temperature", "top_p"):
            if request.options.get(name) is not None:
                model[name] = request.options[name]
        if request.options.get("max_tokens") is not None:
            model["max_output_tokens"] = request.options["max_tokens"]
        return await provider.chat(
            model,
            request.messages,
            request.tools,
            self._resolve_secret(self._model.get("secret", "")),
        )
