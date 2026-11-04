"""MCP tool logic without MCP (T-373 plan §3.9): ask / result / cancel / models.

Every failure becomes an `AskResult` with `verdict="error"` and a code; nothing raises to the
MCP layer. Logs (stderr) carry codes, task ids and sizes only.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from llmhub.mcp_server.checks import Check, CheckSpecError, build_checks, run_checks_async
from llmhub.mcp_server.client import TERMINAL, HubClient, HubError
from llmhub.mcp_server.kinds import Kind
from llmhub.mcp_server.models import AskResult, CheckOut, ErrorInfo, Tokens
from llmhub.mcp_server.paths import PathDenied, Roots
from llmhub.mcp_server.request import InputDoc, build_body, idempotency_key
from llmhub.mcp_server.secret import KeyUnavailable
from llmhub.mcp_server.settings import McpSettings

log = logging.getLogger("llmhub.mcp")

MIN_SUBMIT_BUDGET_S = 5.0

HINT_OK = "Review the result before using it; if it is not acceptable, do the subtask yourself."
HINT_PENDING = (
    "Not finished within wait_s; the task keeps running. Call llmhub_result(task_id, wait_s=...) "
    "later (repeating the same llmhub_ask also joins this task)."
)
HINT_CHECK_FAILED = (
    "The answer failed the checks; it was saved as *.rejected.txt only for inspection. Do the "
    "subtask yourself; do not repair the answer and do not re-ask with fresh=true."
)
HINT_RETRYABLE = "Temporary error: the same call may be repeated after retry_after_s."
HINT_FATAL = "Do the subtask yourself."


class Invalid(Exception):
    """Local refusal before any request; `code` goes to `error.code`."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


@dataclass
class AskParams:
    kind: str = "general"
    input: str = ""
    input_files: list[str] = field(default_factory=list[str])
    schema: dict[str, Any] | str | None = None
    checks: list[str] = field(default_factory=list[str])
    model: str | None = None
    priority: Literal["urgent", "normal", "low"] | None = None
    policy: Literal["ECONOMY", "BALANCED", "QUALITY"] | None = None
    max_tokens: int | None = None
    data_class: Literal["private", "cloud_allowed"] = "private"
    wait_s: float | None = None
    deadline_s: float | None = None
    output_file: str | None = None
    overwrite: bool = False
    fresh: bool = False


def _error(code: str, reason: str | None = None, *, retryable: bool = False,
           retry_after_s: float | None = None, task_id: str | None = None,
           status: str | None = None) -> AskResult:  # fmt: skip
    return AskResult(
        verdict="error", task_id=task_id, status=status,
        error=ErrorInfo(code=code, reason=reason, retryable=retryable, retry_after_s=retry_after_s),
        hint=HINT_RETRYABLE if retryable else HINT_FATAL,
    )  # fmt: skip


def _preview(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class McpService:
    def __init__(
        self, settings: McpSettings, client: HubClient, roots: Roots, kinds: dict[str, Kind]
    ) -> None:
        self.settings = settings
        self.client = client
        self.roots = roots
        self.kinds = kinds

    # --- guarded entry points -----------------------------------------------------------
    async def _guard(self, coro: Any) -> AskResult:
        try:
            return cast(AskResult, await coro)
        except Invalid as exc:
            return _error(exc.code, exc.reason)
        except PathDenied as exc:
            return _error(exc.code)
        except CheckSpecError as exc:
            return _error("invalid_check", str(exc))
        except KeyUnavailable as exc:
            return _error("key_unavailable", exc.reason)
        except HubError as exc:
            return _error(exc.code, exc.reason, retryable=exc.retryable,
                          retry_after_s=exc.retry_after_s, task_id=exc.task_id)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - the agent gets a code, stderr the class
            log.error("llmhub-mcp: internal error %s", type(exc).__name__)
            return _error("internal_error")

    async def ask(self, p: AskParams) -> AskResult:
        return await self._guard(self._ask(p))

    async def result(
        self,
        task_id: str,
        *,
        schema: dict[str, Any] | str | None = None,
        checks: list[str] | None = None,
        kind: str | None = None,
        output_file: str | None = None,
        overwrite: bool = False,
        wait_s: float = 0,
    ) -> AskResult:
        return await self._guard(
            self._result(task_id, schema, checks or [], kind, output_file, overwrite, wait_s)
        )

    async def cancel(self, task_id: str) -> AskResult:
        return await self._guard(self._cancel(task_id))

    async def models(self) -> dict[str, Any]:
        kinds = [
            {"name": k.name, "description": k.description, "requires_schema": k.requires_schema}
            for k in self.kinds.values()
        ]
        out: dict[str, Any] = {"models": [], "kinds": kinds, "error": None}
        try:
            self._need_url()
            items = await self.client.models()
        except Invalid as exc:
            out["error"] = ErrorInfo(code=exc.code, reason=exc.reason).model_dump()
            return out
        except HubError as exc:
            out["error"] = ErrorInfo(code=exc.code, reason=exc.reason, retryable=exc.retryable,
                                     retry_after_s=exc.retry_after_s).model_dump()  # fmt: skip
            return out
        for m in items:
            meta = cast(dict[str, Any], m.get("llmhub") or {})
            out["models"].append({
                "id": m.get("id"), "local": meta.get("local"), "cloud": meta.get("cloud"),
                "ctx_tokens": meta.get("ctx_tokens"),
            })  # fmt: skip
        return out

    # --- helpers ------------------------------------------------------------------------
    def _need_url(self) -> None:
        if not self.settings.url:
            raise Invalid("config_missing", "LLMHUB_URL is not set")

    def _kind(self, name: str | None) -> Kind | None:
        if name is None:
            return None
        kind = self.kinds.get(name)
        if kind is None:
            raise Invalid("invalid_request", "unknown kind")
        return kind

    def _schema(self, schema: dict[str, Any] | str | None) -> dict[str, Any] | None:
        if schema is None or isinstance(schema, dict):
            return schema
        path = self.roots.resolve_input(schema)
        text = self.roots.read_text(path, self.settings.max_input_bytes)
        try:
            data = json.loads(text)
        except ValueError:
            raise Invalid("invalid_request", "schema file is not JSON") from None
        if not isinstance(data, dict):
            raise Invalid("invalid_request", "schema must be a JSON object")
        return cast(dict[str, Any], data)

    def _wait_s(self, wait_s: float | None) -> float:
        value = self.settings.default_wait_s if wait_s is None else wait_s
        if value < 0:
            raise Invalid("invalid_request", "wait_s must be >= 0")
        return min(value, self.settings.max_wait_s)

    def _read_inputs(self, text: str, files: list[str]) -> list[InputDoc]:
        limit = self.settings.max_input_bytes
        used = len(text.encode("utf-8"))
        if used > limit:
            raise Invalid("input_too_large")
        resolved = [self.roots.resolve_input(f) for f in files]
        docs: list[InputDoc] = []
        for path in resolved:
            try:
                body = self.roots.read_text(path, limit - used)
            except PathDenied as exc:
                if exc.code == "input_too_large":
                    raise Invalid("input_too_large") from None
                raise
            used += len(body.encode("utf-8"))
            docs.append(InputDoc(path.name, body))
        return docs

    # --- ask ----------------------------------------------------------------------------
    async def _ask(self, p: AskParams) -> AskResult:
        self._need_url()
        kind = self._kind(p.kind)
        assert kind is not None
        schema = self._schema(p.schema)
        if kind.requires_schema and schema is None:
            raise Invalid("invalid_request", f"kind {kind.name} requires schema")
        checks = build_checks(p.checks, schema, kind_defaults=kind.checks)
        if p.data_class == "cloud_allowed" and not self.settings.allow_cloud:
            raise Invalid("cloud_not_enabled", "LLMHUB_MCP_ALLOW_CLOUD is not set")
        if p.max_tokens is not None and p.max_tokens <= 0:
            raise Invalid("invalid_request", "max_tokens must be > 0")
        if p.deadline_s is not None and p.deadline_s <= 0:
            raise Invalid("invalid_request", "deadline_s must be > 0")
        wait_s = self._wait_s(p.wait_s)
        out_path = (
            self.roots.resolve_output(p.output_file, overwrite=p.overwrite)
            if p.output_file else None
        )  # fmt: skip
        docs = self._read_inputs(p.input, p.input_files)
        if not p.input.strip() and not docs:
            raise Invalid("invalid_request", "empty input")
        body = build_body(kind, p.input, docs, model=p.model or self.settings.default_model,
                          schema=schema, max_tokens=p.max_tokens)  # fmt: skip
        key = idempotency_key(
            body, data_class=p.data_class, priority=p.priority, policy=p.policy,
            nonce=secrets.token_hex(16) if p.fresh else None,
        )  # fmt: skip
        clock = self.client.monotonic
        start = clock()
        sub = await self.client.submit(
            body, idem_key=key, data_class=p.data_class, priority=p.priority, policy=p.policy,
            deadline_s=p.deadline_s, budget_s=max(wait_s, MIN_SUBMIT_BUDGET_S),
        )  # fmt: skip
        try:
            st = await self.client.wait(sub.id, max(0.0, wait_s - (clock() - start)))
            return await self._finish(sub.id, st, checks, out_path, p.overwrite)
        except HubError as exc:  # the task exists: keep its id for llmhub_result
            exc.task_id = exc.task_id or sub.id
            raise

    async def _result(
        self,
        task_id: str,
        schema_raw: dict[str, Any] | str | None,
        specs: list[str],
        kind_name: str | None,
        output_file: str | None,
        overwrite: bool,
        wait_s: float,
    ) -> AskResult:
        self._need_url()
        kind = self._kind(kind_name)
        schema = self._schema(schema_raw)
        checks = build_checks(specs, schema, kind_defaults=kind.checks if kind else ())
        out_path = (
            self.roots.resolve_output(output_file, overwrite=overwrite) if output_file else None
        )
        try:
            st = await self.client.wait(task_id, self._wait_s(wait_s))
            return await self._finish(task_id, st, checks, out_path, overwrite)
        except HubError as exc:
            exc.task_id = exc.task_id or task_id
            raise

    async def _cancel(self, task_id: str) -> AskResult:
        self._need_url()
        r = await self.client.cancel(task_id)
        status = r.get("status") if isinstance(r.get("status"), str) else None
        return AskResult(verdict="ok", task_id=task_id, status=status,
                         hint="Cancellation requested; check with llmhub_result.")  # fmt: skip

    # --- finishing ----------------------------------------------------------------------
    async def _finish(
        self,
        task_id: str,
        st: dict[str, Any],
        checks: list[Check],
        out_path: Path | None,
        overwrite: bool,
    ) -> AskResult:
        status = st.get("status") if isinstance(st.get("status"), str) else None
        if status not in TERMINAL:
            eta = st.get("eta_s")
            return AskResult(
                verdict="pending", task_id=task_id, status=status,
                eta_s=float(eta) if isinstance(eta, int | float) else None, hint=HINT_PENDING,
            )  # fmt: skip
        if status != "completed":
            err = cast(dict[str, Any], st.get("error") or {})
            raw_code = err.get("code")
            code = raw_code if isinstance(raw_code, str) else str(status)
            after = err.get("retry_after_s")
            return _error(
                code, err.get("reason") if isinstance(err.get("reason"), str) else None,
                retryable=bool(err.get("retryable")),
                retry_after_s=float(after) if isinstance(after, int | float) else None,
                task_id=task_id, status=status,
            )  # fmt: skip
        res = await self.client.result(task_id)
        choices = res.body.get("choices")
        choice = cast(dict[str, Any], choices[0]) if isinstance(choices, list) and choices else {}
        message = cast(dict[str, Any], choice.get("message") or {})
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        finish = (
            choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
        )
        usage = cast(dict[str, Any], res.body.get("usage") or {})
        common: dict[str, Any] = {
            "task_id": task_id, "status": status, "finish_reason": finish,
            "truncated": finish == "length" or res.truncated, "backend": res.backend,
            "fallback": res.fallback, "warnings": list(res.warnings),
            "tokens": Tokens(prompt=_as_int(usage.get("prompt_tokens")),
                             completion=_as_int(usage.get("completion_tokens"))),
        }  # fmt: skip
        if finish == "error":
            out = _error("upstream_rejected", "the model server rejected the request",
                         task_id=task_id, status=status)  # fmt: skip
            return out.model_copy(update={k: v for k, v in common.items() if k != "status"})
        results = await run_checks_async(text, finish, checks)
        outs = [CheckOut(name=r.name, ok=r.ok, reason=r.reason) for r in results]
        size = len(text.encode("utf-8"))
        log.info("llmhub-mcp: task %s done, %d bytes", task_id, size)
        if not all(r.ok for r in results):
            rejected = self.roots.scratch_file(f"{task_id}.rejected.txt")
            self.roots.write_text(rejected, text, overwrite=True)
            return AskResult(
                verdict="check_failed", checks=outs, output_file=str(rejected),
                text=_preview(text, self.settings.preview_bytes),
                truncated_inline=size > self.settings.preview_bytes, hint=HINT_CHECK_FAILED,
                **common,
            )  # fmt: skip
        if out_path is not None:
            self.roots.write_text(out_path, text, overwrite=overwrite)
            return AskResult(
                verdict="ok", checks=outs, output_file=str(out_path),
                text=_preview(text, self.settings.preview_bytes),
                truncated_inline=size > self.settings.preview_bytes, hint=HINT_OK, **common,
            )  # fmt: skip
        if size > self.settings.max_inline_bytes:
            spill = self.roots.scratch_file(f"{task_id}.txt")
            self.roots.write_text(spill, text, overwrite=True)
            return AskResult(
                verdict="ok", checks=outs, output_file=str(spill),
                text=_preview(text, self.settings.preview_bytes), truncated_inline=True,
                hint=HINT_OK, **common,
            )  # fmt: skip
        return AskResult(verdict="ok", checks=outs, text=text, hint=HINT_OK, **common)
