"""T-373 C1: task body and Idempotency-Key."""

from __future__ import annotations

import re
from typing import Any

from llmhub.mcp_server.kinds import Kind
from llmhub.mcp_server.request import InputDoc, build_body, idempotency_key

KIND = Kind(name="k", description="d", system="SYS", max_tokens=111)
SCHEMA = {"type": "object"}


def key(body: dict[str, Any], **kw: Any) -> str:
    args: dict[str, Any] = {"data_class": "private", "priority": None, "policy": None,
                            "nonce": None}  # fmt: skip
    args.update(kw)
    return idempotency_key(body, **args)


def test_body_messages_and_file_blocks() -> None:
    body = build_body(KIND, "do it", [InputDoc("a.txt", "AAA"), InputDoc("b.md", "BBB")],
                      model="auto", schema=None, max_tokens=None)  # fmt: skip
    assert body["model"] == "auto" and body["max_tokens"] == 111
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    user = body["messages"][1]
    assert user["role"] == "user"
    assert user["content"] == (
        "do it\n\n<<<FILE a.txt>>>\nAAA\n<<<END FILE>>>\n\n<<<FILE b.md>>>\nBBB\n<<<END FILE>>>"
    )
    assert "response_format" not in body and "chat_template_kwargs" not in body
    assert "llmhub" not in body


def test_body_only_files() -> None:
    body = build_body(KIND, "", [InputDoc("a", "x")], model="m", schema=None, max_tokens=5)
    assert body["messages"][1]["content"] == "<<<FILE a>>>\nx\n<<<END FILE>>>"
    assert body["max_tokens"] == 5


def test_body_schema_think_task_type() -> None:
    kind = Kind(name="k", description="d", system="S", max_tokens=1, think=False,
                task_type="summarize")  # fmt: skip
    body = build_body(kind, "x", [], model="auto", schema=SCHEMA, max_tokens=None)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "result", "schema": SCHEMA, "strict": True},
    }
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["llmhub"] == {"task_type": "summarize"}
    think = build_body(Kind(name="k", description="d", system="S", max_tokens=1, think=True),
                       "x", [], model="auto", schema=None, max_tokens=None)  # fmt: skip
    assert think["chat_template_kwargs"] == {"enable_thinking": True}


def test_same_path_other_content_other_key() -> None:
    a = build_body(KIND, "q", [InputDoc("f.txt", "v1")], model="auto", schema=None, max_tokens=None)
    b = build_body(KIND, "q", [InputDoc("f.txt", "v2")], model="auto", schema=None, max_tokens=None)
    assert key(a) != key(b)


def test_other_dir_same_basename_and_content_same_key() -> None:
    # the body only has the basename, so two directories give the same body
    a = build_body(KIND, "q", [InputDoc("f.txt", "v")], model="auto", schema=None, max_tokens=None)
    b = build_body(KIND, "q", [InputDoc("f.txt", "v")], model="auto", schema=None, max_tokens=None)
    assert key(a) == key(b)


def test_nonce_and_options_change_key() -> None:
    body = build_body(KIND, "q", [], model="auto", schema=None, max_tokens=None)
    base = key(body)
    assert key(body, nonce="n1") != base and key(body, nonce="n1") != key(body, nonce="n2")
    assert key(body, data_class="cloud_allowed") != base
    assert key(body, priority="low") != base
    assert key(body, policy="QUALITY") != base


def test_key_format() -> None:
    k = key(build_body(KIND, "привет", [], model="auto", schema=None, max_tokens=None))
    assert re.fullmatch(r"mcp-[0-9a-f]{64}", k) and len(k) <= 255 and k.isascii()
