"""Small, explicit JSON Schema subset for untrusted extension arguments/results."""
from __future__ import annotations

import json
import math
from typing import Any


class SchemaError(ValueError):
    pass


_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_KEYS = {"type", "description", "enum", "properties", "required", "additionalProperties",
         "items", "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum"}


def _finite(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_schema(schema: Any, depth: int = 0) -> None:
    if depth > 8 or not isinstance(schema, dict) or set(schema) - _KEYS:
        raise SchemaError("unsupported_schema")
    kind = schema.get("type")
    if kind not in _TYPES:
        raise SchemaError("unsupported_schema")
    if "description" in schema and (not isinstance(schema["description"], str) or len(schema["description"]) > 1000):
        raise SchemaError("invalid_schema")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if schema.get("additionalProperties") is not False or not isinstance(properties, dict) or len(properties) > 64:
            raise SchemaError("open_schema")
        if not isinstance(required, list) or any(not isinstance(key, str) or key not in properties for key in required):
            raise SchemaError("invalid_schema")
        for key, child in properties.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 80:
                raise SchemaError("invalid_schema")
            validate_schema(child, depth + 1)
    elif kind == "array":
        if not isinstance(schema.get("maxItems"), int) or isinstance(schema["maxItems"], bool) or not 0 <= schema["maxItems"] <= 1024:
            raise SchemaError("unbounded_schema")
        validate_schema(schema.get("items"), depth + 1)
    elif kind == "string":
        if not isinstance(schema.get("maxLength"), int) or isinstance(schema["maxLength"], bool) or not 0 <= schema["maxLength"] <= 65536:
            raise SchemaError("unbounded_schema")
    allowed = {"type", "description", "enum"} | {
        "object": {"properties", "required", "additionalProperties"},
        "array": {"items", "minItems", "maxItems"},
        "string": {"minLength", "maxLength"},
        "integer": {"minimum", "maximum"}, "number": {"minimum", "maximum"},
    }.get(kind, set())
    if set(schema) - allowed:
        raise SchemaError("invalid_schema")
    for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems"), ("minimum", "maximum")):
        for key in (lower, upper):
            if key in schema:
                value = schema[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
                    raise SchemaError("invalid_schema")
                if key in {"minLength", "maxLength", "minItems", "maxItems"} and (not isinstance(value, int) or value < 0):
                    raise SchemaError("invalid_schema")
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise SchemaError("invalid_schema")
    if "enum" in schema:
        if not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 64:
            raise SchemaError("invalid_schema")
        without_enum = {k: v for k, v in schema.items() if k != "enum"}
        for item in schema["enum"]:
            validate_value(item, without_enum, depth)


def validate_value(value: Any, schema: dict[str, Any], depth: int = 0) -> None:
    if depth > 8:
        raise SchemaError("invalid_value")
    kind = schema["type"]
    matches = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "null": value is None,
    }
    if not matches[kind]:
        raise SchemaError("invalid_value")
    if kind in {"number", "integer"}:
        if not _finite(value) or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise SchemaError("invalid_value")
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        raise SchemaError("invalid_value")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            raise SchemaError("invalid_value")
        for key, item in value.items():
            validate_value(item, properties[key], depth + 1)
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema["maxItems"]:
            raise SchemaError("invalid_value")
        for item in value:
            validate_value(item, schema["items"], depth + 1)
    elif kind == "string" and not schema.get("minLength", 0) <= len(value) <= schema["maxLength"]:
        raise SchemaError("invalid_value")


def bounded_json(value: Any, limit: int = 131072) -> bytes:
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise SchemaError("invalid_json") from exc
    if len(data) > limit:
        raise SchemaError("json_too_large")
    return data
