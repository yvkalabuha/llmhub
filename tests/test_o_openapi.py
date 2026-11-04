"""T-369 O1: docs/openapi.yaml is a fresh export of the app; every route, error, header."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from llmhub.api_gateway import (
    routes_admin,
    routes_health,
    routes_leases,
    routes_openai,
    routes_tasks,
)
from llmhub.api_gateway.errors import ErrorCode

ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = ROOT / "docs" / "openapi.yaml"

PLAN_PATHS = {
    ("post", "/v1/chat/completions"), ("get", "/v1/models"), ("post", "/v1/tasks"),
    ("get", "/v1/tasks/{task_id}"), ("get", "/v1/tasks/{task_id}/result"),
    ("post", "/v1/tasks/{task_id}/cancel"), ("post", "/v1/leases"),
    ("post", "/v1/leases/{lease_id}/renew"), ("post", "/v1/leases/{lease_id}/close"),
    ("get", "/admin/status"), ("get", "/admin/budget"), ("get", "/admin/clients"),
    ("post", "/admin/reload_clients"), ("post", "/admin/hold"), ("post", "/admin/release"),
    ("post", "/admin/kill"), ("post", "/admin/resume"), ("post", "/admin/reload_billing"),
    ("get", "/healthz"), ("get", "/readyz"), ("get", "/metrics"),
}  # fmt: skip


def exporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_openapi", ROOT / "scripts/export_openapi.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = mod
    spec.loader.exec_module(mod)
    return mod


def committed() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(SPEC_FILE.read_text(encoding="utf-8"))
    return data


def test_committed_spec_equals_fresh_export() -> None:
    mod = exporter()
    fresh = mod.render(mod.build_spec())
    assert SPEC_FILE.read_text(encoding="utf-8") == fresh, (
        "docs/openapi.yaml is stale: run `uv run python scripts/export_openapi.py`"
    )


def test_paths_match_the_routers() -> None:
    # FastAPI 0.162 includes routers lazily: read the route modules themselves
    routes: set[tuple[str, str]] = set()
    for module in (routes_openai, routes_tasks, routes_leases, routes_admin, routes_health):
        for r in module.router.routes:
            for m in getattr(r, "methods", ()) or ():
                routes.add((m.lower(), str(getattr(r, "path", ""))))
    spec = committed()
    in_spec = {(m, p) for p, ops in spec["paths"].items() for m in ops}
    assert in_spec == routes
    assert PLAN_PATHS <= in_spec


def test_every_protected_route_documents_401_and_error_schema() -> None:
    spec = committed()
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if path in ("/healthz", "/readyz"):
                assert op["security"] == [] and "401" not in op["responses"]
                continue
            assert "401" in op["responses"], (method, path)
            content = op["responses"]["401"]["content"]["application/json"]["schema"]
            assert content == {"$ref": "#/components/schemas/Error"}
            if path.startswith("/admin") or path == "/metrics":
                assert op["security"] == [{"adminKey": []}] and "403" in op["responses"]
            else:
                assert op["security"] == [{"clientKey": []}]


def test_error_codes_and_headers_documented() -> None:
    spec = committed()
    schemas = spec["components"]["schemas"]
    assert set(schemas["ErrorCode"]["enum"]) == {c.value for c in ErrorCode}
    err = schemas["Error"]["properties"]["error"]["properties"]
    assert {"code", "reason", "retryable", "retry_after_s", "task_id"} <= set(err)
    chat = spec["paths"]["/v1/chat/completions"]["post"]
    req_headers = {p["name"] for p in chat["parameters"] if p["in"] == "header"}
    assert {"Idempotency-Key", "X-LLMHub-Priority", "X-LLMHub-Policy", "X-LLMHub-Deadline",
            "X-LLMHub-Wait", "X-LLMHub-Task-Type", "X-LLMHub-Data-Class",
            "X-LLMHub-Lease"} == req_headers  # fmt: skip
    resp_headers = set(chat["responses"]["200"]["headers"])
    assert {"X-LLMHub-Task", "X-LLMHub-Backend", "X-LLMHub-Fallback", "X-LLMHub-Truncated",
            "X-LLMHub-Cost-Estimate", "X-LLMHub-Warning"} == resp_headers  # fmt: skip
    assert "Retry-After" in chat["responses"]["503"]["headers"]
    for code in ("400", "403", "404", "409", "413", "503", "504"):
        assert code in chat["responses"]
    assert set(spec["paths"]["/v1/tasks"]["post"]["responses"]) >= {"200", "202", "401"}


def test_check_mode_detects_drift(tmp_path: Path) -> None:
    mod = exporter()
    out = tmp_path / "openapi.yaml"
    assert mod.main(["--out", str(out)]) == 0
    assert mod.main(["--check", "--out", str(out)]) == 0
    out.write_text(out.read_text() + "# drift\n")
    assert mod.main(["--check", "--out", str(out)]) == 1
