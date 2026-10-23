"""StrictJsonSerializer for LangGraph checkpoints (ADR-006 §6, ATLAS-L06)."""

from __future__ import annotations

import json
import math
from typing import Any


class StrictSerializationError(ValueError):
    """Raised when a checkpoint value or blob violates strict JSON-only serialization."""


_FORBIDDEN_DICT_KEYS: frozenset[str] = frozenset(
    {
        "lc",
        "__type__",
        "__class__",
        "__reduce__",
        "__reduce_ex__",
        "py/object",
        "py/reduce",
    }
)


def _validate_json_primitive(value: Any, path: str = "$") -> Any:
    """Recursively verify value is a pure JSON primitive and return normalized structure."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise StrictSerializationError(f"NaN/Infinity float rejected at {path}")
        return value
    if isinstance(value, (list, tuple)):
        return [_validate_json_primitive(item, f"{path}[{idx}]") for idx, item in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise StrictSerializationError(
                    f"Non-string dict key {k!r} ({type(k).__name__}) rejected at {path}"
                )
            if k in _FORBIDDEN_DICT_KEYS:
                raise StrictSerializationError(
                    f"Forbidden constructor key {k!r} rejected at {path}"
                )
            out[k] = _validate_json_primitive(v, f"{path}.{k}")
        return out
    raise StrictSerializationError(
        f"Non-JSON type {type(value).__name__} rejected at {path}"
    )


def _reject_non_finite_constant(const: str) -> Any:
    raise StrictSerializationError(f"Non-finite JSON constant {const!r} rejected")


class StrictJsonSerializer:
    """Implements LangGraph's SerializerProtocol using UTF-8 JSON primitives only (ADR-006 §6).

    - Rejects any non-'json' type tag before decoding (blocks pickle, msgpack, etc.).
    - Rejects any dict containing LangChain ('lc') or object constructor tags.
    - Constructs zero custom Python objects on load.
    """

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        validated = _validate_json_primitive(obj)
        try:
            encoded = json.dumps(
                validated,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StrictSerializationError(f"JSON encoding failed: {exc}") from exc
        return ("json", encoded)

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        if not isinstance(data, tuple) or len(data) != 2:
            raise StrictSerializationError("Invalid typed checkpoint tuple")
        type_tag, raw_bytes = data
        if type_tag != "json":
            raise StrictSerializationError(
                f"Rejected non-JSON checkpoint type tag {type_tag!r} before decoding"
            )
        if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
            raise StrictSerializationError("Checkpoint payload must be bytes")
        raw = bytes(raw_bytes)
        # Reject binary pickle/msgpack headers even if mislabelled as 'json'
        if raw.startswith((b"\x80", b"\x91", b"\x92", b"\x93", b"\xc7", b"\xc8", b"\xc9")):
            raise StrictSerializationError("Hostile binary payload header rejected before decoding")
        try:
            text = raw.decode("utf-8")
            parsed = json.loads(text, parse_constant=_reject_non_finite_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StrictSerializationError(f"Invalid UTF-8 JSON in checkpoint: {exc}") from exc
        return _validate_json_primitive(parsed)
