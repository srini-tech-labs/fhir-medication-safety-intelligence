"""schema_check.validate must accept/reject EXACTLY what the reference `jsonschema` library (Draft 2020-12) does for RESPONSE_SCHEMA,
and must fail closed on anything it does not implement. Also: the Nova generation schema is a compatible derivation -- it may accept
MORE than the contract, and the post-response validation is what restores the contract."""
from __future__ import annotations

import copy
import json
import random

import pytest
from jsonschema import Draft202012Validator

from app.services.explanation.bedrock import NOVA_TOOL_SCHEMA, NOVA_TOP_LEVEL_KEYS, generation_schema
from app.services.explanation.claude import RESPONSE_SCHEMA
from app.services.explanation.schema_check import UnsupportedSchema, validate

CONTRACT = Draft202012Validator(RESPONSE_SCHEMA)
NOVA = Draft202012Validator(NOVA_TOOL_SCHEMA)

VALID = [
    {"summary": "s", "findingExplanations": [], "dataGapExplanation": None, "groundedInFindingsOnly": True},
    {"summary": "s", "findingExplanations": [{"ruleId": "DL-001", "explanation": "e"}], "dataGapExplanation": "gap", "groundedInFindingsOnly": False},
    {"summary": "", "findingExplanations": [{"ruleId": "A", "explanation": "x"}, {"ruleId": "B", "explanation": "y"}], "dataGapExplanation": None, "groundedInFindingsOnly": True},
]
JUNK = [None, 0, 1, 2.5, -1, True, False, "", "x", [], [1], {}, {"a": 1}, [{"ruleId": "A"}], [None], "null"]


def mutants(seed: int, n: int):
    rng = random.Random(seed)
    for _ in range(n):
        doc = copy.deepcopy(rng.choice(VALID))
        for _ in range(rng.randint(1, 3)):
            op = rng.choice(["drop", "extra", "junk", "item_drop", "item_extra", "item_junk", "root"])
            items = doc.get("findingExplanations") if isinstance(doc, dict) else None
            item = rng.choice(items) if isinstance(items, list) and items and isinstance(rng.choice(items), dict) else None
            if op == "drop" and isinstance(doc, dict) and doc:
                doc.pop(rng.choice(list(doc)), None)
            elif op == "extra" and isinstance(doc, dict):
                doc[rng.choice(["extra", "risk", "advice"])] = rng.choice(JUNK)
            elif op == "junk" and isinstance(doc, dict) and doc:
                doc[rng.choice(list(doc))] = rng.choice(JUNK)
            elif op == "item_drop" and item:
                item.pop(rng.choice(list(item)), None)
            elif op == "item_extra" and item is not None:
                item["severity"] = rng.choice(JUNK)
            elif op == "item_junk" and item:
                item[rng.choice(list(item))] = rng.choice(JUNK)
            elif op == "root":
                doc = rng.choice(JUNK)
        yield doc


CORPUS = [*VALID, *JUNK, *mutants(20260919, 4000)]


def test_the_validator_agrees_with_the_reference_library_on_a_large_corpus():
    disagreements = [json.dumps(d)[:120] for d in CORPUS if (not validate(d, RESPONSE_SCHEMA)) != CONTRACT.is_valid(d)]
    assert disagreements == []
    verdicts = {CONTRACT.is_valid(d) for d in CORPUS}
    assert verdicts == {True, False}  # the corpus really exercises both outcomes
    assert sum(CONTRACT.is_valid(d) for d in CORPUS) > 50 and sum(not CONTRACT.is_valid(d) for d in CORPUS) > 1000


@pytest.mark.parametrize("doc", [
    {**VALID[0], "extra": 1},                                                  # extra top-level key (the contract forbids it)
    {**VALID[1], "findingExplanations": [{"ruleId": "A", "explanation": "e", "severity": "HIGH"}]},   # extra key inside an item
    {k: v for k, v in VALID[0].items() if k != "dataGapExplanation"},           # missing required key
    {**VALID[0], "dataGapExplanation": 5},                                     # wrong type for the nullable field
    {**VALID[0], "groundedInFindingsOnly": "true"}, {**VALID[0], "summary": None}, {**VALID[0], "findingExplanations": {}},
    {**VALID[0], "findingExplanations": ["not an object"]}, [], "text", None,
])
def test_everything_the_contract_rejects_is_rejected_by_the_post_response_validator(doc):
    assert not CONTRACT.is_valid(doc)
    assert validate(doc, RESPONSE_SCHEMA)


def test_the_validator_never_silently_ignores_what_it_does_not_implement():
    for bad in ({"type": "object", "minProperties": 1}, {"type": "string", "pattern": "^a"}, {"$ref": "#/x"}, {"type": "object", "additionalProperties": {"type": "string"}},
                {"type": "wibble"}):
        with pytest.raises(UnsupportedSchema):
            validate({"a": "b"} if bad.get("type") != "string" else "a", bad)


def test_booleans_are_not_numbers_or_integers():
    assert validate(True, {"type": "number"}) and validate(True, {"type": "integer"}) and not validate(3, {"type": "integer"})


# ---- the Nova generation schema ----------------------------------------------------------------------------------
def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def test_generation_schema_uses_only_what_nova_documents_for_a_tool_input_schema():
    assert set(NOVA_TOOL_SCHEMA) <= NOVA_TOP_LEVEL_KEYS and set(NOVA_TOOL_SCHEMA) == {"type", "properties", "required"}
    assert not any("additionalProperties" in n or "anyOf" in n for n in walk(NOVA_TOOL_SCHEMA))  # removed everywhere, not only at the top
    assert NOVA_TOOL_SCHEMA["properties"]["dataGapExplanation"]["type"] == ["string", "null"]
    assert NOVA_TOOL_SCHEMA["required"] == RESPONSE_SCHEMA["required"]
    assert NOVA_TOOL_SCHEMA["properties"]["findingExplanations"]["items"]["required"] == ["ruleId", "explanation"]
    depth = lambda n, d=0: max([d] + [depth(v, d + 1) for v in (n.values() if isinstance(n, dict) else [])])
    assert json.dumps(NOVA_TOOL_SCHEMA).count('"type": "object"') == 2  # two layers of nesting, as Nova recommends


def test_the_contract_schema_itself_is_left_untouched_by_the_derivation():
    assert RESPONSE_SCHEMA["additionalProperties"] is False and "anyOf" in RESPONSE_SCHEMA["properties"]["dataGapExplanation"]
    assert RESPONSE_SCHEMA["properties"]["findingExplanations"]["items"]["additionalProperties"] is False


def test_the_generation_schema_accepts_everything_the_contract_accepts():
    assert [d for d in CORPUS if CONTRACT.is_valid(d) and not NOVA.is_valid(d)] == []


def test_the_generation_schema_may_accept_more_and_the_post_validator_restores_the_contract():
    looser = [d for d in CORPUS if NOVA.is_valid(d) and not CONTRACT.is_valid(d)]
    assert looser, "the derivation is expected to be a superset (extra keys allowed) -- that is why the contract is re-applied"
    assert all(validate(d, RESPONSE_SCHEMA) for d in looser)  # every such document is still rejected after the response
    assert any(isinstance(d, dict) and set(d) - set(RESPONSE_SCHEMA["properties"]) for d in looser)


def test_the_derivation_refuses_shapes_it_cannot_express():
    with pytest.raises(ValueError, match="anyOf"):
        generation_schema({"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "object", "properties": {}}]}}, "required": []})
    with pytest.raises(ValueError, match="top-level keys"):
        generation_schema({"type": "object", "properties": {}, "required": [], "description": "x", "title": "y"})
