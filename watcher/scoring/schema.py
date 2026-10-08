"""Converts a pydantic-generated JSON schema into the strict subset the
Messages API's structured-output feature (output_config.format) accepts.

client.messages.parse()'s output_format convenience kwarg does this
automatically for a normal call, but Batch API requests are built from raw
params (there's no .parse() equivalent for batches), so this mirrors that
same conversion independently - implemented locally rather than importing the
SDK's own (private, unstable) transform helper. Shared by every Claude
structured-output caller in this project (listing scoring, price-watch
extraction) rather than duplicated per caller.
"""
from __future__ import annotations

from typing import Any, Dict


def strict_json_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    strict: Dict[str, Any] = {}
    schema = dict(schema)

    defs = schema.pop("$defs", None)
    if defs is not None:
        strict["$defs"] = {name: strict_json_schema(value) for name, value in defs.items()}

    ref = schema.pop("$ref", None)
    if ref is not None:
        strict["$ref"] = ref
        return strict

    type_ = schema.pop("type", None)
    any_of = schema.pop("anyOf", None) or schema.pop("oneOf", None)
    if any_of is not None:
        strict["anyOf"] = [strict_json_schema(variant) for variant in any_of]
    else:
        strict["type"] = type_

    enum = schema.pop("enum", None)
    if enum is not None:
        strict["enum"] = enum
    description = schema.pop("description", None)
    if description is not None:
        strict["description"] = description

    if type_ == "object":
        strict["properties"] = {
            key: strict_json_schema(prop) for key, prop in schema.pop("properties", {}).items()
        }
        schema.pop("additionalProperties", None)
        strict["additionalProperties"] = False
        required = schema.pop("required", None)
        if required is not None:
            strict["required"] = required
    elif type_ == "array":
        items = schema.pop("items", None)
        if items is not None:
            strict["items"] = strict_json_schema(items)

    schema.pop("title", None)  # pydantic's own metadata, not needed in the schema
    # Anything else left over (e.g. pydantic's minimum/maximum for a
    # Field(ge=..., le=...)) isn't a key the API's schema format supports -
    # fold it into the description as a hint instead of silently dropping it.
    if schema:
        description = strict.get("description")
        strict["description"] = (
            (description + "\n\n" if description else "")
            + "{" + ", ".join(f"{key}: {value}" for key, value in schema.items()) + "}"
        )
    return strict
