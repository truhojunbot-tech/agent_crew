"""Frozen receipt schema — the one place a receipt is checked against the contract.

The schema file (``tests/cea_contract/receipt.schema.json``) is a **byte-identical
copy** of alfred ``sev0/cea-alfred-lineage`` @ ``e1063eb``
``tests/cea_contract/receipt.schema.json`` (blob ``41e7ebf``). It is the shared
contract between the alfred cron adapter and this admission engine; it is closed
(``additionalProperties: false``) and every §3 + Π field is required. Do not edit
it here — a change needs a new ADR revision (ADR §0.3 freeze rule).

Validation uses ``jsonschema`` when it is importable and otherwise falls back to
:func:`_validate_subset`, a self-contained checker for exactly the JSON-Schema
constructs this file uses (``type``, ``enum``, ``const``, ``required``,
``properties``, ``additionalProperties: false``, ``anyOf``, ``$ref`` into
``$defs``, ``pattern``, ``minLength``, ``minimum``, ``items``). The unit tests
assert the two agree on every fixture, so the fallback is not a second contract.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

def _schema_path() -> Path:
    """Locate the frozen schema.

    Primary location is the repo copy (``src/agent_crew/cea`` → repo root →
    ``tests/cea_contract``). A sibling copy next to this module is accepted so an
    installed package can carry the contract, but the repo copy wins: there is
    one contract, and the test suite pins its blob hash.
    """
    here = Path(__file__).resolve()
    repo_copy = here.parents[3] / "tests" / "cea_contract" / "receipt.schema.json"
    if repo_copy.exists():
        return repo_copy
    return here.parent / "receipt.schema.json"


SCHEMA_PATH = _schema_path()

SCHEMA_SOURCE = "alfred sev0/cea-alfred-lineage @e1063eb tests/cea_contract/receipt.schema.json"
SCHEMA_BLOB = "41e7ebf271830790f6aae80a413e51edf7805fcd"
"""``git hash-object`` of the frozen file; the copy here must match it byte for byte."""


@lru_cache(maxsize=1)
def load_schema() -> dict:
    """The frozen schema as a dict. Cached; the file never changes at runtime."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_receipt(receipt: Any) -> list[str]:
    """Return a list of contract violations; empty list means the receipt conforms.

    Never raises for a malformed receipt — a validator that throws cannot be used
    inside a gate transaction.
    """
    schema = load_schema()
    try:
        import jsonschema  # noqa: PLC0415 — optional dependency, checked at call time
    except Exception:
        return _validate_subset(receipt, schema, schema, "$")
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{_jsonschema_path(err)}: {err.message}"
            for err in sorted(validator.iter_errors(receipt), key=lambda e: list(e.path))]


def _jsonschema_path(err: Any) -> str:
    parts = ["$"] + [str(p) for p in err.path]
    return ".".join(parts)


# ── fallback checker ────────────────────────────────────────────────────────

def _validate_subset(value: Any, node: dict, root: dict, path: str) -> list[str]:
    """Check ``value`` against ``node``; supports only the constructs the frozen
    schema uses. Unknown keywords are ignored rather than silently passing a
    different contract — the test suite pins the keyword set."""
    errors: list[str] = []
    if "$ref" in node:
        return _validate_subset(value, _deref(node["$ref"], root), root, path)
    if "anyOf" in node:
        for branch in node["anyOf"]:
            if not _validate_subset(value, branch, root, path):
                return []
        return [f"{path}: matches none of anyOf"]
    if "const" in node and value != node["const"]:
        errors.append(f"{path}: {value!r} is not {node['const']!r}")
    if "enum" in node and value not in node["enum"]:
        errors.append(f"{path}: {value!r} is not one of {node['enum']!r}")
    if "type" in node:
        types = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not any(_is_type(value, t) for t in types):
            errors.append(f"{path}: {value!r} is not of type {types}")
            return errors
    if isinstance(value, str):
        if "pattern" in node and not re.search(node["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {node['pattern']!r}")
        if "minLength" in node and len(value) < node["minLength"]:
            errors.append(f"{path}: shorter than minLength {node['minLength']}")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            errors.append(f"{path}: {value} < minimum {node['minimum']}")
    if isinstance(value, dict):
        props = node.get("properties", {})
        for key in node.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        if node.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}: unexpected key {key!r} (closed schema)")
        for key, sub in props.items():
            if key in value:
                errors.extend(_validate_subset(value[key], sub, root, f"{path}.{key}"))
    if isinstance(value, list) and "items" in node:
        for i, item in enumerate(value):
            errors.extend(_validate_subset(item, node["items"], root, f"{path}[{i}]"))
    return errors


def _is_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _deref(ref: str, root: dict) -> dict:
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        if not part:
            continue
        node = node[part]
    return node


def schema_required_fields() -> tuple[str, ...]:
    """Top-level required keys, in schema order — the receipt-store column set."""
    return tuple(load_schema()["required"])


def optional_fields() -> tuple[str, ...]:
    """Keys the closed schema permits but does not require (today: ``provenance``)."""
    required = set(schema_required_fields())
    return tuple(k for k in load_schema()["properties"] if k not in required)


def canonical_json(receipt: Any) -> str:
    """Stable serialisation used for storage and for hashing a receipt body."""
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def first_error(receipt: Any) -> Optional[str]:
    errs = validate_receipt(receipt)
    return errs[0] if errs else None
