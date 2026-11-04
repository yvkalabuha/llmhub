"""MCP tools over the llmhub API (T-373 plan §3.10, §6).

The tool descriptions are the main place of the delegation rule: agents read them.
"""

# No `from __future__ import annotations`: MCPServer evaluates the tool signatures, which use
# closure variables (kind names, wait limits) in their descriptions.
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from llmhub.mcp_server.client import HubClient
from llmhub.mcp_server.kinds import Kind, load_kinds
from llmhub.mcp_server.models import AskResult
from llmhub.mcp_server.paths import Roots
from llmhub.mcp_server.secret import Secret, load_key
from llmhub.mcp_server.service import AskParams, McpService
from llmhub.mcp_server.settings import McpSettings

INSTRUCTIONS = (
    "llmhub runs a slow local LLM (and, only if allowed, a cloud GPU). Delegate only "
    "structure extraction with a `schema` and classification by an explicit field; Claude must "
    "review every result. Any verdict other than `ok` means: do the subtask yourself, do not "
    "repair the answer. Never put secrets or keys into the input. Paths are absolute and must be "
    "inside the configured roots; dot-files and dot-directories are never readable."
)

ASK_RULES = """\
Send a task to llmhub (local LLM, CPU, ~minutes) and check the answer on this side.

WHEN TO USE: only (1) extracting structure from data into JSON by `schema` where every value \
can be verified against the source (kind=extract_json, plus checks like item-count:N), and \
(2) classification by an explicit field or rule (kind=classify, `schema` with an enum). Worth it \
for large inputs (>= 8k tokens) you would otherwise read yourself.
NOT FOR: release notes, changelogs, translations, free-form classification, numeric aggregates, \
code review as a verdict, anything with secrets or keys in the input, anything where an error \
would not be caught by a check or by your own review.
RESULT: `verdict` is ok | check_failed | pending | error. ok -> review the result before use; \
if it is not acceptable, do the subtask yourself. check_failed or error -> do the subtask \
yourself; do not repair the answer and do not re-ask with fresh=true to get another answer \
(only error.retryable=true may be repeated after error.retry_after_s). pending -> call \
llmhub_result(task_id, wait_s=...) later; the task keeps running.
FILES: input_files / output_file / schema-as-path are absolute paths inside the allowed roots; \
large answers are written to a scratch file (`output_file`, `truncated_inline=true`, `text` is \
the beginning). data_class defaults to private (local only).
A call lasts at most max(wait_s, 5) + ~20 s; keep that below the MCP tool timeout of the \
client (MCP_TOOL_TIMEOUT); for long tasks use wait_s=0..30 and llmhub_result.
"""


def ask_description(kinds: dict[str, Kind]) -> str:
    lines = [ASK_RULES, "KINDS:"]
    lines += [f"- {k.name}: {k.description}" for k in kinds.values()]
    return "\n".join(lines)


def _key_loader(settings: McpSettings) -> Callable[[], Secret]:
    cache: list[Secret] = []

    def load() -> Secret:
        if not cache:
            cache.append(load_key(settings.key_env, settings.key_file))
        return cache[0]

    return load


def build_server(
    settings: McpSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> MCPServer:
    kinds = load_kinds(settings.kinds_file)
    extra: dict[str, Any] = {}
    if sleep is not None:
        extra["sleep"] = sleep
    if monotonic is not None:
        extra["monotonic"] = monotonic
    client = HubClient(settings.url or "http://unset.invalid", _key_loader(settings),
                       timeout_s=settings.http_timeout_s, transport=transport, **extra)  # fmt: skip
    roots = Roots.build(settings.roots, settings.scratch)
    service = McpService(settings, client, roots, kinds)

    server = MCPServer("llmhub", instructions=INSTRUCTIONS, log_level="WARNING")
    kind_names = ", ".join(kinds)

    @server.tool(
        name="llmhub_ask",
        description=ask_description(kinds),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    async def llmhub_ask(
        kind: Annotated[str, Field(description=f"Task kind: {kind_names}")] = "general",
        input: Annotated[str, Field(description="Instruction and/or inline data")] = "",
        input_files: Annotated[
            list[str], Field(description="Absolute paths of UTF-8 text files inside the roots")
        ] = [],  # noqa: B006
        schema: Annotated[
            dict[str, Any] | str | None,
            Field(
                description="JSON Schema of the answer (object) or absolute path to a .json file; "
                "required for extract_json"
            ),
        ] = None,
        checks: Annotated[
            list[str],
            Field(
                description="Extra checks: max-bytes:N, sections:A,B, item-count:N, "
                "script:<latin|cyrillic|han>:<ratio>, regex:<re>, no-regex:<re> "
                "(not-truncated is always on)"
            ),
        ] = [],  # noqa: B006
        model: Annotated[str | None, Field(description="Model alias (default: auto)")] = None,
        priority: Literal["urgent", "normal", "low"] | None = None,
        policy: Literal["ECONOMY", "BALANCED", "QUALITY"] | None = None,
        max_tokens: Annotated[int | None, Field(gt=0)] = None,
        data_class: Annotated[
            Literal["private", "cloud_allowed"],
            Field(
                description="private = local only (default); cloud_allowed works only if enabled "
                "for this MCP server and for the llmhub client"
            ),
        ] = "private",
        wait_s: Annotated[
            float | None,
            Field(
                ge=0,
                description=(
                    f"Seconds to wait (default {settings.default_wait_s:g}, max "
                    f"{settings.max_wait_s:g}); then verdict=pending"
                ),
            ),
        ] = None,
        deadline_s: Annotated[
            float | None,
            Field(
                gt=0, description=("Fail the task if it has not started within this many seconds")
            ),
        ] = None,
        output_file: Annotated[
            str | None,
            Field(description="Absolute path inside the roots to write the answer to (ok only)"),
        ] = None,
        overwrite: bool = False,
        fresh: Annotated[
            bool, Field(description="Start a new run of an identical request instead of joining")
        ] = False,
    ) -> AskResult:
        return await service.ask(AskParams(
            kind=kind, input=input, input_files=list(input_files), schema=schema,
            checks=list(checks), model=model, priority=priority, policy=policy,
            max_tokens=max_tokens, data_class=data_class, wait_s=wait_s, deadline_s=deadline_s,
            output_file=output_file, overwrite=overwrite, fresh=fresh,
        ))  # fmt: skip

    @server.tool(
        name="llmhub_result",
        description=(
            "Get (and check) the result of an llmhub task started by llmhub_ask, waiting up to "
            "wait_s. Pass the same kind/schema/checks as the ask to check it the same way; "
            "verdict rules as in llmhub_ask."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
        structured_output=True,
    )
    async def llmhub_result(
        task_id: str,
        kind: Annotated[
            str | None, Field(description=f"Kind for default checks: {kind_names}")
        ] = None,
        schema: dict[str, Any] | str | None = None,
        checks: list[str] = [],  # noqa: B006
        output_file: str | None = None,
        overwrite: bool = False,
        wait_s: Annotated[float, Field(ge=0)] = 0,
    ) -> AskResult:
        return await service.result(task_id, schema=schema, checks=list(checks), kind=kind,
                                    output_file=output_file, overwrite=overwrite, wait_s=wait_s)  # fmt: skip

    @server.tool(
        name="llmhub_cancel",
        description="Cancel an llmhub task (a running cloud task is still paid for).",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
        structured_output=True,
    )
    async def llmhub_cancel(task_id: str) -> AskResult:
        return await service.cancel(task_id)

    @server.tool(
        name="llmhub_models",
        description="List llmhub model aliases (local/cloud, context size) and the task kinds.",
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
        structured_output=True,
    )
    async def llmhub_models() -> dict[str, Any]:
        return await service.models()

    return server
