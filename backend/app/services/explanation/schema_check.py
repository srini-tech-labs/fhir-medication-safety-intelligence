"""Strict validation of a model's structured output against ``RESPONSE_SCHEMA`` -- the authoritative application contract.

Some providers (Amazon Nova tool use) can only be given a *generation* schema that is a compatible subset of the contract (no
``additionalProperties``, no ``anyOf``). Their output is therefore re-validated here against the ORIGINAL schema before the
grounding guard sees it, so everything the contract rejects (extra keys, wrong types, a missing key) is rejected regardless of
what the generation schema allowed.

A deliberately small validator for exactly the JSON-Schema keywords the contract uses. It FAILS CLOSED: a schema keyword it does
not implement raises ``UnsupportedSchema`` instead of being silently ignored. ``tests/unit/test_schema_check.py`` proves it agrees
with the reference ``jsonschema`` library (Draft 2020-12) on a large generated corpus of valid and invalid documents.
"""
from __future__ import annotations

ANNOTATIONS = {"description", "title", "$comment"}
SUPPORTED = {"type", "properties", "required", "additionalProperties", "items", "anyOf", "enum", "const"}


class UnsupportedSchema(ValueError):
    """The schema uses a keyword this validator does not implement (never treated as 'valid')."""


def _is_type(value, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise UnsupportedSchema(f"unknown type {name!r}")


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    """Return the list of violations (empty = valid)."""
    unknown = set(schema) - SUPPORTED - ANNOTATIONS
    if unknown:
        raise UnsupportedSchema(f"unsupported schema keyword(s) at {path}: {sorted(unknown)}")
    if schema.get("additionalProperties") not in (None, True, False):
        raise UnsupportedSchema(f"additionalProperties at {path} must be a boolean")  # a schema-valued form is not implemented
    problems: list[str] = []

    if "anyOf" in schema and not any(not validate(instance, sub, path) for sub in schema["anyOf"]):
        problems.append(f"{path}: matches none of the allowed alternatives")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(instance, t) for t in types):
            return problems + [f"{path}: expected {'|'.join(types)}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{path}: not one of the allowed values")
    if "const" in schema and instance != schema["const"]:
        problems.append(f"{path}: differs from the required constant")

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                problems.append(f"{path}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    problems.append(f"{path}: unexpected key {key!r}")
        for key, sub in props.items():
            if key in instance:
                problems += validate(instance[key], sub, f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            problems += validate(item, schema["items"], f"{path}[{i}]")
    return problems
