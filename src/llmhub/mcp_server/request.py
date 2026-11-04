"""Task body and Idempotency-Key (T-373 plan §2.5, §2.6, §3.6)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from llmhub.mcp_server.kinds import Kind


@dataclass(frozen=True)
class InputDoc:
    name: str  # basename only: the path never reaches llmhub and never affects the key
    text: str


def user_text(input_text: str, docs: Sequence[InputDoc]) -> str:
    parts = [input_text] if input_text else []
    parts += [f"<<<FILE {d.name}>>>\n{d.text}\n<<<END FILE>>>" for d in docs]
    return "\n\n".join(parts)


def build_body(
    kind: Kind,
    input_text: str,
    docs: Sequence[InputDoc],
    *,
    model: str,
    schema: dict[str, Any] | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": kind.system},
            {"role": "user", "content": user_text(input_text, docs)},
        ],
        "max_tokens": max_tokens if max_tokens is not None else kind.max_tokens,
    }
    if kind.think is not None:
        body["chat_template_kwargs"] = {"enable_thinking": kind.think}
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema, "strict": True},
        }
    if kind.task_type is not None:
        body["llmhub"] = {"task_type": kind.task_type}
    return body


def idempotency_key(
    body: Mapping[str, Any],
    *,
    data_class: str,
    priority: str | None,
    policy: str | None,
    nonce: str | None,
) -> str:
    canonical = json.dumps(
        {"body": body, "data_class": data_class, "priority": priority, "policy": policy,
         "nonce": nonce},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )  # fmt: skip
    return "mcp-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
