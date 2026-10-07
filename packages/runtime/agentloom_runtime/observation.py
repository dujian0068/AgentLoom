"""Detached, immutable data for observers; execution objects are never observations."""

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

ObservationScalar = str | int | float | bool | None


def freeze_observation(value: Any) -> Any:
    """Copy JSON-shaped data into read-only mappings and tuples.

    Only plain dictionaries with string keys, lists/tuples and finite JSON scalars
    are accepted. In particular, this never serializes arbitrary execution objects
    through their attributes, callbacks, repr, or custom serialization methods.
    """
    active: set[int] = set()

    def freeze(item):
        if item is None or type(item) in (str, int, bool):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("Observation numbers must be finite")
            return item
        if type(item) not in (dict, list, tuple):
            raise TypeError("Observations require JSON-shaped data, not execution objects")
        identity = id(item)
        if identity in active:
            raise ValueError("Observations cannot contain cyclic data")
        active.add(identity)
        try:
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise TypeError("Observation mapping keys must be strings")
                return MappingProxyType({key: freeze(child) for key, child in item.items()})
            return tuple(freeze(child) for child in item)
        finally:
            active.remove(identity)

    return freeze(value)


def snapshot_metadata(
    metadata: Mapping[str, ObservationScalar] | None,
) -> dict[str, ObservationScalar]:
    """Capture explicitly supplied scalar labels without retaining their container."""
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("Observation metadata must be a mapping of scalar labels")
    result = {}
    for key, value in metadata.items():
        if type(key) is not str or (
            value is not None and type(value) not in (str, int, float, bool)
        ):
            raise TypeError("Observation metadata requires string keys and scalar values")
        result[key] = freeze_observation(value)
    return result
