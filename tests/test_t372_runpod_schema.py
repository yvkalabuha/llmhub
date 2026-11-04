"""T-372 A1b: the fields RunpodAdapter sends and reads exist in RunPod's openapi.json.

The real file (`tests/fixtures/runpod/openapi.json`, downloaded without a key in P0) is
checked when present; the coordinator requires that test green before any paid step. Without
it the real-file test is skipped; the checker itself is tested on a synthetic document.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from llmhub.configuration.models import RunpodConfig
from llmhub.provider_adapters.runpod import create_body
from runpod_fake import make_spec
from t372_scripts import load

ROOT = Path(__file__).resolve().parent.parent
rsc = load("runpod_schema_check")

REAL = Path(__file__).parent / "fixtures" / "runpod" / "openapi.json"


def _obj(*fields: str, **typed: Any) -> dict[str, Any]:
    props: dict[str, Any] = {f: {"type": "string"} for f in fields}
    props.update(typed)
    return {"type": "object", "properties": props}


def _ok(schema: dict[str, Any]) -> dict[str, Any]:
    return {"200": {"content": {"application/json": {"schema": schema}}}}


def _params(*names: str) -> list[dict[str, Any]]:
    return [{"name": n, "in": "query"} for n in names]


POD = {"$ref": "#/components/schemas/Pod"}
SYNTH: dict[str, Any] = {
    "openapi": "3.0.3",
    "components": {"schemas": {
        "Pod": _obj("id", "name", "costPerHr", "machineId", "image", "lastStartedAt",
                    "volumeInGb", "interruptible", "networkVolume",
                    desiredStatus={"type": "string", "enum": ["RUNNING", "EXITED", "TERMINATED"]}),
        "PodCreateInput": {"allOf": [_obj(
            "name", "imageName", "gpuTypeIds", "gpuCount", "containerDiskInGb", "volumeInGb",
            "ports", "interruptible", "env", "dockerStartCmd", "networkVolumeId", "templateId",
            cloudType={"type": "string", "enum": ["SECURE", "COMMUNITY"]},
            computeType={"type": "string", "enum": ["GPU", "CPU"]},
            allowedCudaVersions={"type": "array", "items": {"type": "string",
                                 "enum": ["12.4", "12.8", "12.9", "13.0"]}},
        )]},
    }},
    "paths": {
        "/pods": {
            "get": {"responses": _ok({"type": "array", "items": POD})},
            "post": {"requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/PodCreateInput"}}}},
                "responses": {"201": {"content": {"application/json": {"schema": POD}}}}},
        },
        "/pods/{podId}": {"get": {"responses": _ok(POD)}, "delete": {"responses": {}}},
        "/networkvolumes": {"get": {"responses": _ok({"type": "array",
                                                      "items": _obj("id", "name")})}},
        "/templates": {"get": {"parameters": _params("includePublicTemplates",
                                                     "includeRunpodTemplates"),
                               "responses": _ok({"type": "array", "items": _obj("id", "name")})}},
        "/billing/pods": {"get": {"parameters": _params("startTime", "endTime", "bucketSize",
                                                        "grouping"),
                                  "responses": _ok({"type": "array",
                                                    "items": _obj("amount", "time", "podId")})}},
    },
}  # fmt: skip


def test_synthetic_document_passes() -> None:
    assert rsc.check(SYNTH) == []
    assert "createdAt" not in rsc.POD_FIELDS + rsc.POD_OPTIONAL_FIELDS  # not in the real Pod


def test_checker_lists_every_body_field_the_adapter_sends() -> None:
    body = create_body(make_spec(), "llmhub-0123456789ab-01234567", RunpodConfig(),
                       "NVIDIA GeForce RTX 3090", docker_start_cmd=["sleep", "600"])  # fmt: skip
    assert set(body) <= set(rsc.CREATE_FIELDS)


def _mutate(path: list[str], value: Any = None, *, delete: bool = False) -> dict[str, Any]:
    doc = copy.deepcopy(SYNTH)
    node: Any = doc
    for k in path[:-1]:
        node = node[k]
    if delete:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        (_mutate(["components", "schemas", "PodCreateInput", "allOf", 0, "properties",  # type: ignore[list-item]
                  "allowedCudaVersions"], delete=True), "allowedCudaVersions"),
        (_mutate(["components", "schemas", "Pod", "properties", "costPerHr"], delete=True),
         "costPerHr"),
        (_mutate(["components", "schemas", "Pod", "properties", "lastStartedAt"], delete=True),
         "lastStartedAt"),
        (_mutate(["components", "schemas", "Pod", "properties", "volumeInGb"], delete=True),
         "volumeInGb"),
        (_mutate(["components", "schemas", "Pod", "properties", "desiredStatus"],
                 {"type": "string", "enum": ["RUNNING", "STARTING"]}), "STARTING"),
        (_mutate(["paths", "/pods", "get", "responses"], _ok(_obj("data", "nextCursor"))),
         "paging"),
        (_mutate(["paths", "/billing/pods", "get", "parameters"], _params("startTime")),
         "bucketSize"),
        (_mutate(["components", "schemas", "PodCreateInput", "allOf", 0, "properties",  # type: ignore[list-item]
                  "cloudType"], {"type": "string", "enum": ["SECURE"]}), "COMMUNITY"),
    ],
)
def test_checker_finds_drift(doc: dict[str, Any], needle: str) -> None:
    problems = rsc.check(doc)
    assert any(needle in p for p in problems), problems


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "openapi.json"
    f.write_text(json.dumps(SYNTH))
    assert rsc.main([str(f)]) == 0 and "schema: ok" in capsys.readouterr().out
    f.write_text("{}")
    assert rsc.main([str(f)]) == 2


@pytest.mark.skipif(not REAL.exists(), reason="P0: download openapi.json (no key) first")
def test_real_openapi_matches_adapter() -> None:
    doc = json.loads(REAL.read_text())
    assert rsc.check(doc) == []
