#!/usr/bin/env python3
"""Check RunpodAdapter field names against RunPod's public `openapi.json` (T-372 A1b, P0).

    curl -fsS https://rest.runpod.io/v1/openapi.json -o tests/fixtures/runpod/openapi.json
    uv run python scripts/runpod_schema_check.py tests/fixtures/runpod/openapi.json

No key is needed or used. Exit 0 = every field the adapter sends or reads exists in the schema
(and the enums contain our values); 1 = problems (printed one per line); 2 = unreadable file.
`tests/test_t372_runpod_schema.py` runs the same check on the file when it is present.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# What the adapter sends / reads (keep in sync with llmhub.provider_adapters.runpod{,_api})
CREATE_FIELDS = (
    "name", "imageName", "cloudType", "computeType", "gpuTypeIds", "gpuCount",
    "containerDiskInGb", "volumeInGb", "ports", "interruptible", "allowedCudaVersions", "env",
    "dockerStartCmd",
)  # fmt: skip
# the real Pod has no `createdAt` (R2-0/REV-A1); `lastStartedAt` (the time source) and the
# money fields checked after create (REV-S10) must exist in the schema, but a pod may omit them
POD_FIELDS = ("id", "name", "desiredStatus", "costPerHr", "machineId")
POD_OPTIONAL_FIELDS = ("lastStartedAt", "volumeInGb", "interruptible", "networkVolume")
POD_IMAGE_FIELDS = ("image", "imageName")  # one of them
NAMED_FIELDS = ("id", "name")
BILLING_FIELDS = ("amount", "time", "podId")
BILLING_PARAMS = ("startTime", "endTime", "bucketSize", "grouping")
TEMPLATE_PARAMS = ("includePublicTemplates", "includeRunpodTemplates")
KNOWN_STATUSES = {"RUNNING", "CREATED", "RESTARTING", "EXITED", "TERMINATED"}
ENUMS = {
    "cloudType": {"COMMUNITY"},
    "computeType": {"GPU"},
    "allowedCudaVersions": {"12.8", "12.9", "13.0"},
}
POD_ITEM = re.compile(r"^/pods/\{[^}/]+\}$")


class Spec:
    def __init__(self, doc: Mapping[str, Any]) -> None:
        self.doc = doc

    def resolve(self, node: Any, depth: int = 0) -> Any:
        while isinstance(node, dict) and "$ref" in node and depth < 32:
            ref = str(node["$ref"])
            if not ref.startswith("#/"):
                return {}
            cur: Any = self.doc
            for part in ref[2:].split("/"):
                cur = cur.get(part, {}) if isinstance(cur, dict) else {}
            node = cur
            depth += 1
        return node

    def props(self, schema: Any, depth: int = 0) -> dict[str, Any]:
        """Properties of an object schema (allOf/anyOf/oneOf merged, $ref resolved)."""
        s = self.resolve(schema)
        if not isinstance(s, dict) or depth > 16:
            return {}
        out: dict[str, Any] = dict(s.get("properties", {}) or {})
        for key in ("allOf", "anyOf", "oneOf"):
            for sub in s.get(key, []) or []:
                out.update(self.props(sub, depth + 1))
        return out

    def items(self, schema: Any) -> Any | None:
        s = self.resolve(schema)
        if isinstance(s, dict) and s.get("type") == "array":
            return s.get("items")
        return None

    def enum(self, schema: Any) -> set[str] | None:
        s = self.resolve(schema)
        if not isinstance(s, dict):
            return None
        if "enum" in s:
            return {str(x) for x in s["enum"]}
        if s.get("type") == "array":
            return self.enum(s.get("items"))
        for key in ("allOf", "anyOf", "oneOf"):
            for sub in s.get(key, []) or []:
                e = self.enum(sub)
                if e is not None:
                    return e
        return None

    def op(self, path: str, method: str) -> Mapping[str, Any] | None:
        node = self.doc.get("paths", {}).get(path, {})
        node = node.get(method) if isinstance(node, dict) else None
        return node if isinstance(node, dict) else None

    def path_like(self, pattern: re.Pattern[str]) -> str | None:
        return next((p for p in self.doc.get("paths", {}) if pattern.match(p)), None)

    def response(self, op: Mapping[str, Any] | None) -> Any:
        if op is None:
            return None
        resp = op.get("responses", {})
        for code in ("200", "201", 200, 201):
            if code in resp:
                r = self.resolve(resp[code])
                content = r.get("content", {}) if isinstance(r, dict) else {}
                return (content.get("application/json") or {}).get("schema")
        return None

    def request(self, op: Mapping[str, Any] | None) -> Any:
        if op is None:
            return None
        body = self.resolve(op.get("requestBody", {}))
        content = body.get("content", {}) if isinstance(body, dict) else {}
        return (content.get("application/json") or {}).get("schema")

    def params(self, op: Mapping[str, Any] | None) -> set[str]:
        if op is None:
            return set()
        return {str(self.resolve(p).get("name")) for p in op.get("parameters", []) or []}


def check(doc: Mapping[str, Any]) -> list[str]:
    """Problems found (empty = the adapter matches the schema)."""
    s = Spec(doc)
    problems: list[str] = []

    def missing(where: str, have: Mapping[str, Any] | set[str], need: tuple[str, ...]) -> None:
        for f in need:
            if f not in have:
                problems.append(f"{where}: no field {f!r}")

    create = s.op("/pods", "post")
    if create is None:
        problems.append("POST /pods: not in paths")
    cprops = s.props(s.request(create))
    missing("POST /pods body", cprops, CREATE_FIELDS)
    for field, values in ENUMS.items():
        if field in cprops:
            enum = s.enum(cprops[field])
            if enum is not None and not values <= enum:
                problems.append(f"POST /pods body: {field} enum lacks {sorted(values - enum)}")

    item_path = s.path_like(POD_ITEM)
    if item_path is None:
        problems.append("GET /pods/{id}: not in paths")
    else:
        for method in ("get", "delete"):
            if s.op(item_path, method) is None:
                problems.append(f"{method.upper()} {item_path}: not in paths")
    pod_schema = s.response(s.op(item_path, "get")) if item_path else None
    for where, schema in (("GET /pods/{id}", pod_schema), ("POST /pods", s.response(create))):
        pprops = s.props(schema)
        missing(f"{where} answer", pprops, POD_FIELDS + POD_OPTIONAL_FIELDS)
        if not any(f in pprops for f in POD_IMAGE_FIELDS):
            problems.append(f"{where} answer: neither 'image' nor 'imageName'")
        if "desiredStatus" in pprops:
            enum = s.enum(pprops["desiredStatus"])
            if enum is not None and enum - KNOWN_STATUSES:
                problems.append(f"{where}: unknown desiredStatus {sorted(enum - KNOWN_STATUSES)}")

    for path, fields in (("/pods", POD_FIELDS), ("/networkvolumes", NAMED_FIELDS),
                         ("/templates", NAMED_FIELDS), ("/billing/pods", BILLING_FIELDS)):  # fmt: skip
        op = s.op(path, "get")
        if op is None:
            problems.append(f"GET {path}: not in paths")
            continue
        items = s.items(s.response(op))
        if items is None:
            problems.append(f"GET {path}: answer is not a JSON array (paging shape: review "
                            "RunpodApi._list)")  # fmt: skip
            continue
        missing(f"GET {path} item", s.props(items), fields)
    missing("GET /billing/pods params", s.params(s.op("/billing/pods", "get")), BILLING_PARAMS)
    missing("GET /templates params", s.params(s.op("/templates", "get")), TEMPLATE_PARAMS)
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        doc = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {argv[0]}: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not isinstance(doc, dict) or "paths" not in doc:
        print("not an OpenAPI document (no 'paths')", file=sys.stderr)
        return 2
    problems = check(doc)
    for p in problems:
        print(p)
    print("schema: ok" if not problems else f"schema: {len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
