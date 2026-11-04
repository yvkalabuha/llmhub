"""Export the normative OpenAPI document: `uv run python scripts/export_openapi.py [--check]`.

Paths and methods come from the FastAPI app itself (`create_app(...).openapi()`); this script
adds what the routes cannot express because they read raw JSON: request/response schemas, the
error object, the `X-LLMHub-*` and `Idempotency-Key` headers and the error codes per route
(T-369 plan section 4). `docs/openapi.yaml` is committed; `tests/test_o_openapi.py` fails when
it differs from a fresh export. `--check` exits 1 on a difference instead of writing.
"""

from __future__ import annotations

import argparse
import copy
import sys
import types
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "openapi.yaml"

ERR = {"$ref": "#/components/schemas/Error"}


SHOULD_RETRY = {
    "x-should-retry": {
        "description": "true|false = error.retryable; stops openai-python's own retries "
        "of non-retryable 409/5xx errors",
        "schema": {"type": "string", "enum": ["true", "false"]},
    }
}


def _err(desc: str) -> dict[str, Any]:
    return {"description": desc, "content": {"application/json": {"schema": ERR}},
            "headers": SHOULD_RETRY}  # fmt: skip


def _obj(props: dict[str, Any], required: list[str] | None = None, extra: bool = True) -> dict:
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    if not extra:
        out["additionalProperties"] = False
    return out


S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
B = {"type": "boolean"}
NS = {"type": ["string", "null"]}
NN = {"type": ["number", "null"]}

SCHEMAS: dict[str, Any] = {
    "Error": _obj({"error": _obj({
        "message": S,
        "type": {"type": "string", "enum": ["authentication_error", "permission_error",
                                            "invalid_request_error", "server_error"]},
        "param": {"type": "null"},
        "code": {"$ref": "#/components/schemas/ErrorCode"},
        "reason": {**NS, "description": "cause, e.g. one of the six cloud-unavailability "
                   "reasons: insufficient_balance, quota_exceeded, provider_api_error, "
                   "gpu_unavailable, deployment_error, budget_limit_reached"},
        "retryable": B,
        "retry_after_s": NN,
        "task_id": {**NS, "description": "the task keeps running; poll /v1/tasks/{id}"},
        "fallback": NS,
    }, ["message", "type", "code", "retryable"])}, ["error"]),
    "ErrorCode": {"type": "string", "enum": [
        "unauthorized", "forbidden", "invalid_request", "request_too_large",
        "context_too_large", "model_not_available", "not_found", "idempotency_conflict",
        "task_not_ready", "lease_expired", "lease_closed", "conflict", "queue_wait_exceeded",
        "cloud_unavailable", "local_unavailable", "controller_halted", "busy",
        "deadline_exceeded", "upstream_timeout", "cancelled", "internal_error"]},
    "LlmhubOptions": _obj({
        "priority": {"type": "string", "enum": ["urgent", "normal", "low"]},
        "policy": {"type": "string", "enum": ["ECONOMY", "BALANCED", "QUALITY"]},
        "deadline_s": N, "task_type": S,
        "data_class": {"type": "string", "enum": ["private", "cloud_allowed"],
                       "description": "can only narrow the client's class to private"},
    }, extra=False),
    "ChatRequest": {
        **_obj({
            "model": {**S, "description": "alias from GET /v1/models or 'auto'"},
            "messages": {"type": "array", "items": {"type": "object"}, "minItems": 1},
            "stream": B,
            "stream_options": _obj({"include_usage": B}),
            "max_tokens": I, "max_completion_tokens": I, "tools": {"type": "array"},
            "response_format": {"type": "object"},
            "llmhub": {"$ref": "#/components/schemas/LlmhubOptions"},
        }, ["model", "messages"]),
        "description": "OpenAI chat body; unknown fields pass through to the model server.",
    },
    "ChatCompletion": _obj({
        "id": S, "object": {"const": "chat.completion"}, "created": I, "model": S,
        "choices": {"type": "array", "items": _obj({
            "index": I, "finish_reason": S,
            "message": _obj({"role": S, "content": NS, "reasoning_content": NS,
                             "tool_calls": {"type": "array"}})})},
        "usage": _obj({"prompt_tokens": I, "completion_tokens": I, "total_tokens": I}),
    }, ["id", "object", "choices"]),
    "ModelList": _obj({"object": {"const": "list"}, "data": {"type": "array", "items": _obj({
        "id": S, "object": {"const": "model"}, "created": I, "owned_by": S,
        "llmhub": _obj({"local": B, "cloud": B, "ctx_tokens": {"type": ["integer", "null"]},
                        "alias_of": S})})}}),
    "Task": _obj({
        "id": S, "object": {"const": "llmhub.task"},
        "status": {"type": "string", "enum": ["submitted", "queued", "running", "completed",
                                              "failed", "cancelled"]},
        "route": S, "backend": NS, "created_at": N,
    }, ["id", "status"]),
    "TaskStatus": _obj({
        "id": S, "object": S, "status": S, "route": S, "backend": NS, "pool": NS,
        "priority": S, "policy": S, "created_at": N, "started_at": NN, "finished_at": NN,
        "eta_s": NN, "est_cost_usd": NN, "cost_usd": NN, "finish_reason": NS,
        "usage": {"type": ["object", "null"]}, "fallback": NS,
        "warnings": {"type": "array", "items": S},
        "error": {"type": ["object", "null"], "description": "error object of failed tasks"},
    }),
    "CancelResult": _obj({"id": S, "status": S, "cancel_requested": B}),
    "LeaseRequest": _obj({"model": S, "ttl_s": {"type": "number", "minimum": 30,
                                                "maximum": 3600}, "prewarm": B}, ["model"]),
    "Lease": _obj({"id": S, "pool": S, "kind": {"type": "string", "enum": ["explicit",
                   "prewarm"]}, "ttl_s": N, "expires_at": N}),
    "LeaseRenewed": _obj({"id": S, "expires_at": N}),
    "LeaseClose": _obj({"final": B}),
    "LeaseClosed": _obj({"id": S, "closed": B}),
    "PoolOrModel": _obj({"pool": S, "model": S, "ttl_s": N}),
    "Health": _obj({"status": S, "version": S, "halted": B, "uptime_s": N}),
    "Ready": _obj({"ready": B, "reason": S}),
    "AnyObject": {"type": "object"},
}  # fmt: skip

H_REQ = {
    "Idempotency-Key": "1..255 printable ASCII; kept 7 days; the same key with another body "
    "-> 409 idempotency_conflict. Without it, an identical body of the same client is joined "
    "for ~15 min.",
    "X-LLMHub-Priority": "urgent | normal | low (not above the client's max_priority)",
    "X-LLMHub-Policy": "ECONOMY | BALANCED | QUALITY",
    "X-LLMHub-Deadline": "seconds from now; a task still queued after it fails with "
    "deadline_exceeded",
    "X-LLMHub-Wait": "synchronous window in seconds, 1..480 (default 480)",
    "X-LLMHub-Task-Type": "task type from the server config (max_tokens, thinking)",
    "X-LLMHub-Data-Class": "private | cloud_allowed; can only narrow to private",
    "X-LLMHub-Lease": "id of an explicit lease the request runs under",
}
H_RESP = {
    "X-LLMHub-Task": "task id of this answer",
    "X-LLMHub-Backend": "local:<backend> or cloud:<pool>",
    "X-LLMHub-Fallback": "set when a cloud-eligible request was served by a fallback; value "
    "= the cloud-unavailability reason (or local_unavailable when the cloud took over)",
    "X-LLMHub-Truncated": "1 when finish_reason is length",
    "X-LLMHub-Cost-Estimate": "estimated cloud cost in USD",
    "X-LLMHub-Warning": "comma-separated: secret_detected, economy_stage, ...",
}
RESP_HEADERS = {k: {"description": v, "schema": {"type": "string"}} for k, v in H_RESP.items()}
RETRY_AFTER = {"Retry-After": {"description": "seconds", "schema": {"type": "integer"}}}

E401 = _err("unauthorized: missing or invalid API key")
E403A = _err("forbidden: admin key required (or admin API disabled)")


def _json(ref: str, desc: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "description": desc,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}},
    }
    if headers:
        out["headers"] = headers
    return out


def _body(ref: str, required: bool = True) -> dict[str, Any]:
    return {
        "required": required,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}},
    }


CHAT_ERRORS = {
    "400": _err("invalid_request | context_too_large"),
    "403": _err("forbidden (model, priority, private data without a local model)"),
    "404": _err("model_not_available"),
    "409": _err("idempotency_conflict (task_id of the existing task)"),
    "413": _err("request_too_large"),
    "503": {**_err("queue_wait_exceeded (Retry-After, task_id; the task stays) | "
                   "cloud_unavailable (reason) | local_unavailable | controller_halted | "
                   "busy"), "headers": {**SHOULD_RETRY, **RETRY_AFTER}},
    "504": _err("deadline_exceeded | upstream_timeout"),
}  # fmt: skip

ROUTES: dict[tuple[str, str], dict[str, Any]] = {
    ("post", "/v1/chat/completions"): {
        "summary": "OpenAI chat completion (sync wait <= 480 s, optional SSE stream)",
        "description": "stream=true: headers at once, ': keep-alive' comments every 15 s, the "
        "answer as chat.completion.chunk events at the end (v1 buffers), then [DONE]. An error "
        "after the headers arrives as `data: {\"error\": ...}` without [DONE].",
        "headers": list(H_REQ),
        "requestBody": _body("ChatRequest"),
        "responses": {
            "200": {
                "description": "chat.completion (or text/event-stream when stream=true)",
                "headers": RESP_HEADERS,
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ChatCompletion"}},
                    "text/event-stream": {"schema": {"type": "string"}},
                },
            },
            "204": {"description": "client disconnected; the task keeps running"},
            **CHAT_ERRORS,
        },
    },
    ("get", "/v1/models"): {
        "summary": "Aliases allowed to this client (+ auto)",
        "responses": {"200": _json("ModelList", "model list")},
    },
    ("post", "/v1/tasks"): {
        "summary": "Submit an asynchronous task",
        "headers": [h for h in H_REQ if h != "X-LLMHub-Wait"],
        "requestBody": _body("ChatRequest"),
        "responses": {
            "202": _json("Task", "created", {"Location": {"schema": {"type": "string"}}}),
            "200": _json("Task", "duplicate (same Idempotency-Key or identical body)"),
            **{k: v for k, v in CHAT_ERRORS.items() if k not in ("504",)},
        },
    },
    ("get", "/v1/tasks/{task_id}"): {
        "summary": "Task status (ETA, route, cost, error)",
        "responses": {"200": _json("TaskStatus", "status"),
                      "404": _err("not_found (other clients' tasks too)")},
    },
    ("get", "/v1/tasks/{task_id}/result"): {
        "summary": "Task result",
        "responses": {
            "200": {**_json("ChatCompletion", "chat.completion"), "headers": RESP_HEADERS},
            "404": _err("not_found"),
            "409": {**_err("task_not_ready (retry_after_s) | cancelled"),
                    "headers": {**SHOULD_RETRY, **RETRY_AFTER}},
            "503": _err("error of the failed task: cloud_unavailable, local_unavailable, "
                        "queue_wait_exceeded, ..."),
            "504": _err("deadline_exceeded | upstream_timeout"),
        },
    },
    ("post", "/v1/tasks/{task_id}/cancel"): {
        "summary": "Cancel (running cloud task: GPU time already spent is paid)",
        "responses": {"200": _json("CancelResult", "cancel state"), "404": _err("not_found")},
    },
    ("post", "/v1/leases"): {
        "summary": "Explicit (or prewarm) lease: keeps the cloud GPU while renewed",
        "requestBody": _body("LeaseRequest"),
        "responses": {
            "201": _json("Lease", "created"),
            "400": _err("invalid_request (ttl_s outside 30..3600)"),
            "403": _err("forbidden (client without cloud_allowed, model not allowed)"),
            "404": _err("model_not_available | not_found (no cloud pool)"),
        },
    },
    ("post", "/v1/leases/{lease_id}/renew"): {
        "summary": "Heartbeat: renew the lease TTL",
        "responses": {
            "200": _json("LeaseRenewed", "renewed"),
            "404": _err("not_found"), "409": _err("lease_closed"), "410": _err("lease_expired"),
        },
    },
    ("post", "/v1/leases/{lease_id}/close"): {
        "summary": "Close the lease (idempotent); final=true drains the GPU",
        "requestBody": _body("LeaseClose", required=False),
        "responses": {"200": _json("LeaseClosed", "closed"), "404": _err("not_found")},
    },
    ("get", "/admin/status"): {"summary": "Pools, local backends, budget, providers, incidents"},
    ("get", "/admin/budget"): {"summary": "Budget stage, committed amounts, warnings"},
    ("get", "/admin/clients"): {"summary": "Clients (no key hashes)"},
    ("post", "/admin/reload_clients"): {
        "summary": "Re-read the clients section (same as SIGHUP)",
        "extra": {"400": _err("invalid_request: config rejected, old clients kept")},
    },
    ("post", "/admin/hold"): {
        "summary": "Admin hold of a pool (<= 8 h)", "requestBody": _body("PoolOrModel"),
        "ok": "201", "extra": {"400": _err("invalid_request"), "404": _err("not_found"),
                               "409": _err("conflict: hold rejected (reason)")},
    },
    ("post", "/admin/release"): {"summary": "Release admin holds",
                                 "requestBody": _body("PoolOrModel")},
    ("post", "/admin/kill"): {"summary": "HALT and destroy every resource (<= 60 s)",
                              "requestBody": _body("AnyObject", required=False)},
    ("post", "/admin/resume"): {
        "summary": "Lift HALTED and provider blocks",
        "requestBody": _body("AnyObject", required=False),
        "extra": {"409": _err("conflict: kill_file_present | reconcile_required")},
    },
    ("post", "/admin/reload_billing"): {
        "summary": "Provider billing since month start -> ledger",
        "extra": {"503": _err("busy")},
    },
    ("post", "/admin/clear_weights_hold"): {"summary": "Lift a weights_hold launch block",
                                            "requestBody": _body("PoolOrModel")},
    ("get", "/healthz"): {"summary": "Liveness (no key, no LLM dependency)",
                          "public": True,
                          "responses": {"200": _json("Health", "ok")}},
    ("get", "/readyz"): {"summary": "Readiness: controller heartbeat <= 60 s and DB open",
                         "public": True,
                         "responses": {"200": _json("Ready", "ready"),
                                       "503": _json("Ready", "not ready")}},
    ("get", "/metrics"): {
        "summary": "Prometheus metrics (admin key)",
        "responses": {"200": {"description": "text/plain; version=0.0.4",
                              "content": {"text/plain": {"schema": {"type": "string"}}}}},
    },
}  # fmt: skip


def app_paths() -> dict[str, Any]:
    from llmhub import __version__
    from llmhub.api_gateway.app import Runtime, create_app

    rt = cast(Runtime, types.SimpleNamespace(version=__version__))
    return cast(dict[str, Any], create_app(rt, run_loops=False).openapi())


def build_spec() -> dict[str, Any]:
    base = app_paths()
    paths: dict[str, Any] = {}
    for path, ops in base["paths"].items():
        for method, op in ops.items():
            meta = ROUTES.get((method, path))
            if meta is None:
                raise SystemExit(f"route {method.upper()} {path} is not described in ROUTES")
            out: dict[str, Any] = {"summary": meta["summary"], "operationId": op["operationId"]}
            if "description" in meta:
                out["description"] = meta["description"]
            params = [copy.deepcopy(p) for p in op.get("parameters", [])]
            for name in meta.get("headers", []):
                params.append({"name": name, "in": "header", "required": False,
                               "description": H_REQ[name], "schema": {"type": "string"}})  # fmt: skip
            if params:
                out["parameters"] = params
            if "requestBody" in meta:
                out["requestBody"] = meta["requestBody"]
            if "responses" in meta:
                responses = dict(meta["responses"])
            else:
                ok = meta.get("ok", "200")
                responses = {ok: _json("AnyObject", "ok"), **meta.get("extra", {})}
            if not meta.get("public"):
                responses.setdefault("401", E401)
                if path.startswith("/admin") or path == "/metrics":
                    responses.setdefault("403", E403A)
                    out["security"] = [{"adminKey": []}]
                else:
                    out["security"] = [{"clientKey": []}]
            else:
                out["security"] = []
            out["responses"] = dict(sorted(responses.items()))
            paths.setdefault(path, {})[method] = out
    missing = set(ROUTES) - {(m, p) for p, ops in paths.items() for m in ops}
    if missing:
        raise SystemExit(f"ROUTES describes routes the app does not have: {sorted(missing)}")
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "llmhub API",
            "version": base["info"]["version"],
            "description": "OpenAI-compatible gateway plus tasks, leases and admin of the "
            "llmhub hybrid LLM dispatcher. Normative contract; generated by "
            "scripts/export_openapi.py. Guide: docs/integration.md.",
        },
        "servers": [{"url": "http://10.8.0.1:8095", "description": "VPN only"}],
        "paths": paths,
        "components": {
            "schemas": SCHEMAS,
            "securitySchemes": {
                "clientKey": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "client key (lh_...)",
                },
                "adminKey": {"type": "http", "scheme": "bearer", "description": "admin key"},
            },
        },
    }


def render(spec: dict[str, Any]) -> str:
    header = "# GENERATED by scripts/export_openapi.py - do not edit by hand\n"
    return header + yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    text = render(build_spec())
    if args.check:
        same = args.out.exists() and args.out.read_text(encoding="utf-8") == text
        print("openapi: up to date" if same else "openapi: differs from a fresh export")
        return 0 if same else 1
    args.out.write_text(text, encoding="utf-8")
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
