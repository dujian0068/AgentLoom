"""Versioned, durable Embedding boundaries for document and query preprocessing."""

import math
from copy import deepcopy

from .hooks import HookRecoveryRequired, execute_operation
from .hooks.registry import identifier, json_copy


def normalize_embedding_response(response, count, *, dimensions=None):
    """Validate actual vectors and restore the provider's indexed input ordering."""
    try:
        vectors = response["vectors"]
        indices = response["indices"]
        if (
            not isinstance(vectors, list)
            or not isinstance(indices, list)
            or len(vectors) != count
            or len(indices) != count
            or any(type(index) is not int for index in indices)
            or sorted(indices) != list(range(count))
            or not count
        ):
            raise ValueError()
        ordered = [vector for _, vector in sorted(zip(indices, vectors, strict=True))]
        if not isinstance(ordered[0], list) or not ordered[0]:
            raise ValueError()
        dimension = len(ordered[0])
        if dimensions is not None and dimension != dimensions:
            raise ValueError()
        if "dimension" in response and (
            type(response["dimension"]) is not int or response["dimension"] != dimension
        ):
            raise ValueError()
        for vector in ordered:
            if not isinstance(vector, list) or len(vector) != dimension:
                raise ValueError()
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                raise ValueError()
        result = {
            "vectors": deepcopy(ordered),
            "indices": list(range(count)),
            "dimension": dimension,
        }
        if "usage" in response:
            if response["usage"] is not None and type(response["usage"]) is not dict:
                raise ValueError()
            result["usage"] = response["usage"]
        if "provider_request_id" in response:
            if response["provider_request_id"] is not None and not isinstance(
                response["provider_request_id"], str
            ):
                raise ValueError()
            result["provider_request_id"] = response["provider_request_id"]
        return json_copy(result)
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        raise RuntimeError("向量响应的数量、索引或维度不合法") from None


class EmbeddingHookBoundary:
    """One persisted batch, with no implicit inheritance from an Agent's Hooks.

    Input texts use stable indexed entries at the Hook boundary. A patch may
    replace each entry's text, but cannot remove, insert or reorder entry IDs.
    ``invoke`` receives only the resulting ``list[str]``; provider credentials
    remain in its host-owned closure. ``state`` and ``save`` belong to the host.
    """

    def __init__(
        self,
        hooks,
        *,
        model_id,
        index_signature,
        dimensions=None,
        expected_dimensions=None,
        scope=None,
    ):
        identifier(model_id, "Embedding model ID")
        # Legacy indexes use base_url + model_id rather than a short hash. Keep
        # those identities intact while bounding the resulting Hook scope size.
        if (
            not isinstance(index_signature, str)
            or not index_signature.strip()
            or len(index_signature.encode("utf-8")) > 4096
        ):
            raise ValueError("Embedding index signature must be nonempty and at most 4096 bytes")
        if dimensions is not None and (type(dimensions) is not int or dimensions <= 0):
            raise ValueError("Embedding dimensions must be a positive integer")
        if expected_dimensions is not None and (
            type(expected_dimensions) is not int or expected_dimensions <= 0
        ):
            raise ValueError("Expected Embedding dimensions must be a positive integer")
        if dimensions is not None and expected_dimensions not in (None, dimensions):
            raise ValueError("Requested and indexed Embedding dimensions do not match")
        self.hooks = hooks
        self.model_id = model_id
        self.index_signature = index_signature
        self.dimensions = dimensions
        self.expected_dimensions = expected_dimensions
        self.scope = deepcopy(scope or {})

    async def invoke(self, texts, invoke, *, state, save, purpose="document", retry_unknown=False):
        if type(retry_unknown) is not bool:
            raise ValueError("Embedding retry_unknown must be an explicit boolean")
        if (
            not isinstance(texts, list)
            or not texts
            or any(not isinstance(text, str) or not text.strip() for text in texts)
        ):
            raise ValueError("Embedding texts must be a nonempty list of nonempty strings")
        if purpose not in {"document", "query"}:
            raise ValueError("Embedding purpose must be document or query")
        original = {
            "texts": [{"index": index, "text": text} for index, text in enumerate(texts)],
            "model_id": self.model_id,
            "index_signature": self.index_signature,
            "dimensions": self.dimensions,
            "encoding_format": "float",
            "purpose": purpose,
        }
        if "original_input" in state and state["original_input"] != original:
            raise HookRecoveryRequired("Embedding batch differs from its saved operation")

        def validate_input(payload):
            if set(payload) != set(original) or any(
                payload[key] != original[key] for key in original if key != "texts"
            ):
                raise ValueError("Embedding Hook cannot change the frozen model or index identity")
            entries = payload["texts"]
            if not isinstance(entries, list) or len(entries) != len(texts):
                raise ValueError("Embedding Hook must preserve the input count")
            for index, entry in enumerate(entries):
                if (
                    type(entry) is not dict
                    or set(entry) != {"index", "text"}
                    or type(entry["index"]) is not int
                    or entry["index"] != index
                    or not isinstance(entry["text"], str)
                    or not entry["text"].strip()
                ):
                    raise ValueError("Embedding Hook must preserve indexed text order")

        async def action(payload):
            response = await invoke([entry["text"] for entry in payload["texts"]])
            if not isinstance(response, dict):
                return {"invalid_response": "embedding_response_must_be_an_object"}
            # Only the public Embedding response contract crosses into extension
            # checkpoints; arbitrary transport metadata and credentials do not.
            actual = {
                key: deepcopy(response[key])
                for key in ("vectors", "indices", "dimension", "usage", "provider_request_id")
                if key in response
            }
            try:
                return json_copy(actual)
            except (TypeError, ValueError, OverflowError):
                # A returned non-JSON fact (e.g. NaN) is still a completed call.
                # Record that invalid fact durably so recovery cannot resend it.
                return {"invalid_response": "embedding_response_is_not_checkpointable_json"}

        expected = self.expected_dimensions or self.dimensions
        result = await execute_operation(
            self.hooks,
            "model.embedding",
            original,
            action,
            scope={
                **self.scope,
                "purpose": purpose,
                "target": self.model_id,
                "model_id": self.model_id,
                "index_signature": self.index_signature,
            },
            state=state,
            save=save,
            validate_input=validate_input,
            validate_prepared=validate_input,
            validate_output=lambda response: normalize_embedding_response(
                response, len(texts), dimensions=expected
            ),
            resume_inflight=retry_unknown and state.get("actual_status") in {"running", "unknown"},
        )
        # Learned dimension is host state, not part of the immutable request. A
        # completed operation still has to match it when admitted to an index.
        return normalize_embedding_response(result, len(texts), dimensions=expected)
