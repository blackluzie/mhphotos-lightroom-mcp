"""Explicitly submitted, bounded collection-name hierarchy. No catalog commands."""

import re

MAX_COLLECTIONS = 1000
MAX_COLLECTION_DEPTH = 8
MAX_COLLECTION_BODY = 1048576
COLLECTION_KINDS = frozenset({"collection", "smart_collection", "collection_set"})
_ID = re.compile(r"node[1-9][0-9]{0,3}")


def validate_collections(payload: object) -> list:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"collections", "truncated"}
        or not isinstance(payload["truncated"], bool)
    ):
        raise ValueError("invalid_collection_payload")
    records = payload["collections"]
    if not isinstance(records, list) or len(records) > MAX_COLLECTIONS:
        raise ValueError("invalid_collection_count")
    seen = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or not {"id", "name", "kind"} <= set(record)
            or not set(record) <= {"id", "name", "kind", "parentId"}
        ):
            raise ValueError("invalid_collection_fields")
        identifier, name, kind = record["id"], record["name"], record["kind"]
        if (
            not isinstance(identifier, str)
            or not _ID.fullmatch(identifier)
            or identifier in seen
            or not isinstance(name, str)
            or not 1 <= len(name) <= 80
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or not isinstance(kind, str)
            or kind not in COLLECTION_KINDS
        ):
            raise ValueError("invalid_collection_values")
        parent = record.get("parentId")
        depth = 1
        if "parentId" in record:
            if (
                not isinstance(parent, str)
                or parent not in seen
                or seen[parent][0] != "collection_set"
            ):
                raise ValueError("invalid_collection_parent")
            depth = seen[parent][1] + 1
        if depth > MAX_COLLECTION_DEPTH:
            raise ValueError("invalid_collection_depth")
        seen[identifier] = (kind, depth)
    return records
