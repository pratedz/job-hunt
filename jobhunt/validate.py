"""Minimal JSON Schema checks, stdlib only.

Supports exactly the keywords our own schemas use: ``type``, ``enum``,
``required``, ``additionalProperties``, ``properties``, ``items``,
``minLength``, ``pattern``. Anything else in a schema file is documentation.

Used twice: to reject a bad ``add --json`` file before it reaches the store, and
to reject a codex reply that does not match ``schemas/score_batch.schema.json``.
"""

from __future__ import annotations

import re


class SchemaError(ValueError):
    """The instance does not match the schema."""


_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _is(value, type_name: str) -> bool:
    if type_name in ("integer", "number"):
        if isinstance(value, bool):  # bool is an int subclass; never a number here
            return False
        return isinstance(value, _TYPES[type_name])
    if type_name == "boolean":
        return isinstance(value, bool)
    return isinstance(value, _TYPES[type_name])


def _same_value(a, b) -> bool:
    """Enum comparison that does not confuse True with 1 or 1 with 1.0-strings."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return type(a) is type(b) and a == b


def validate(schema: dict, instance, path: str = "$") -> None:
    """Raise SchemaError on the first mismatch."""
    if not isinstance(schema, dict):
        return

    types = schema.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else list(types)
        if not any(_is(instance, t) for t in allowed):
            raise SchemaError(f"{path}: expected {'|'.join(allowed)}, got {_name(instance)}")

    if "enum" in schema:
        if not any(_same_value(instance, v) for v in schema["enum"]):
            raise SchemaError(f"{path}: {instance!r} is not one of {schema['enum']!r}")

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                raise SchemaError(f"{path}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    raise SchemaError(f"{path}: unexpected property {key!r}")
        for key, sub in props.items():
            if key in instance:
                validate(sub, instance[key], f"{path}.{key}")

    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            validate(schema["items"], item, f"{path}[{i}]")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(f"{path}: shorter than minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            raise SchemaError(f"{path}: does not match pattern {schema['pattern']!r}")


def _name(value) -> str:
    for name, typ in _TYPES.items():
        if _is(value, name):
            return name
    return type(value).__name__
