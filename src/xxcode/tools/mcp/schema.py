"""JSON Schema to Pydantic conversion for MCP tool inputs."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

logger = logging.getLogger(__name__)

_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}

_CONSTRAINT_MAP: dict[str, str] = {
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
    "minLength": "min_length",
    "maxLength": "max_length",
    "pattern": "pattern",
}


class _EmptyInput(BaseModel):
    """Empty input for tools that need no parameters."""


def _sanitize_model_name(name: str) -> str:
    """Build a safe Python class name from a tool or property name."""
    safe = re.sub(r"[^a-zA-Z0-9]", "_", name)
    if safe and safe[0].isdigit():
        safe = "_" + safe
    return safe or "McpDynamicInput"


def build_mcp_input_model(tool_name: str, json_schema: dict[str, Any]) -> type[BaseModel]:
    """Build a Pydantic model from an MCP tool's inputSchema.

    Supports common JSON Schema features used by MCP tools:
    basic types, nested objects, arrays with item schemas, enum, nullable
    type arrays, simple numeric/string constraints, and
    ``additionalProperties: false``.
    """
    props = json_schema.get("properties", {})
    required_fields: set[str] = set(json_schema.get("required", []))

    if not props:
        return _EmptyInput

    field_defs, _nested = _convert_properties(
        props,
        required_fields,
        prefix=tool_name,
    )
    if not field_defs:
        return _EmptyInput

    model_name = _sanitize_model_name(tool_name)
    extra_kwargs: dict[str, Any] = {}
    if json_schema.get("additionalProperties") is False:
        extra_kwargs["__config__"] = ConfigDict(extra="forbid")

    return create_model(model_name, **field_defs, **extra_kwargs, __base__=BaseModel)


def _convert_properties(
    props: dict[str, Any],
    required_fields: set[str],
    *,
    prefix: str = "",
) -> tuple[dict[str, Any], dict[str, type[BaseModel]]]:
    """Convert JSON Schema properties to Pydantic field definitions."""
    field_defs: dict[str, Any] = {}
    nested_models: dict[str, type[BaseModel]] = {}

    for prop_name, prop_def in props.items():
        if not isinstance(prop_def, dict):
            continue

        py_type, nested, field_kwargs = _convert_property(
            prop_name,
            prop_def,
            prefix=prefix,
        )
        nested_models.update(nested)

        if prop_name in required_fields:
            field_defs[prop_name] = (py_type, Field(**field_kwargs))
        else:
            field_defs[prop_name] = (
                py_type | None,
                Field(default=None, **field_kwargs),
            )

    return field_defs, nested_models


def _convert_property(
    name: str,
    schema: dict[str, Any],
    *,
    prefix: str = "",
) -> tuple[type, dict[str, type[BaseModel]], dict[str, Any]]:
    """Convert one JSON Schema property to Pydantic type/field metadata."""
    field_kwargs: dict[str, Any] = {}
    if "description" in schema:
        field_kwargs["description"] = schema["description"]

    raw_type = schema.get("type", _infer_type(schema))
    is_nullable = False
    if isinstance(raw_type, list):
        types = [t for t in raw_type if t != "null"]
        is_nullable = "null" in raw_type
        json_type = types[0] if types else "string"
    else:
        json_type = raw_type

    if any(k in schema for k in ("oneOf", "anyOf", "allOf")):
        logger.debug(
            "Property '%s' uses oneOf/anyOf/allOf - falling back to Any",
            name,
        )
        return Any, {}, field_kwargs

    if "enum" in schema:
        enum_values = tuple(schema["enum"])
        lit_type = Literal[enum_values]  # type: ignore[valid-type]
        if is_nullable:
            return lit_type | None, {}, field_kwargs
        return lit_type, {}, field_kwargs

    if json_type == "object" and "properties" in schema:
        nested_name = f"{prefix}_{name}" if prefix else name
        nested_model = build_mcp_input_model(nested_name, schema)
        if is_nullable:
            return nested_model | None, {nested_name: nested_model}, field_kwargs
        return nested_model, {nested_name: nested_model}, field_kwargs

    if json_type == "array":
        items_schema = schema.get("items")
        nested_models: dict[str, type[BaseModel]] = {}
        if isinstance(items_schema, dict):
            item_type, item_nested, _ = _convert_property(
                f"{name}_item",
                items_schema,
                prefix=prefix,
            )
            nested_models.update(item_nested)
        else:
            item_type = Any

        py_type = list[item_type]  # type: ignore[valid-type]
        if is_nullable:
            return py_type | None, nested_models, field_kwargs
        return py_type, nested_models, field_kwargs

    py_type = _JSON_TYPE_MAP.get(json_type, Any)
    for js_key, py_key in _CONSTRAINT_MAP.items():
        if js_key in schema:
            field_kwargs[py_key] = schema[js_key]

    if is_nullable:
        return py_type | None, {}, field_kwargs
    return py_type, {}, field_kwargs


def _infer_type(schema: dict[str, Any]) -> str:
    """Guess the JSON Schema type when ``type`` is omitted."""
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    if "enum" in schema:
        return "string"
    return "string"


__all__ = ["_EmptyInput", "build_mcp_input_model"]
