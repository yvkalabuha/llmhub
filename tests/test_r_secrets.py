"""R1: secret detector - every pattern, every request location, timing."""

from __future__ import annotations

import time
from typing import Any

import pytest

from llmhub.model_router.secrets import detect_secrets, extract_strings


def req(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


# (pattern name, positive sample, negative sample) - all fictitious
CASES = [
    (
        "private_key_pem",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE",
        "-----BEGIN PUBLIC KEY-----\nMIIE",
    ),
    (
        "openssh_private_key",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl",
        "ssh-ed25519 AAAAC3Nza me@host",
    ),
    (
        "pgp_private_key",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQ",
        "-----BEGIN PGP PUBLIC KEY BLOCK-----",
    ),
    ("aws_access_key", "key AKIAABCDEFGHIJKLMNOP end", "AKIA is a prefix, AKIAshort"),
    ("sk_key", "use sk-abcdefghij0123456789ABCD now", "the task-force and sk-short"),
    ("github_token", "ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_ is a prefix"),
    ("slack_token", "xoxb-1234567890-abcdefghij", "xoxo hugs"),
    ("google_api_key", "AIza" + "A" * 35, "AIza" + "A" * 10),
    ("hf_token", "hf_" + "a" * 34, "hf_short"),
    ("runpod_key", "rpa_" + "A" * 24, "rpa_x"),
    ("bearer_token", "Authorization: Bearer abcdefghij0123456789xyz", "Bearer of bad news"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkw.abcdefghijkl", "eyJ only one part"),
    ("uri_credentials", "postgres://user:s3cretpw@db.local/x", "https://example.com/path:80@"),
    ("key_name_value", '{"api_key": "abc123def456"}', "the token is in the next section"),
]


@pytest.mark.parametrize(("name", "pos", "neg"), CASES, ids=[c[0] for c in CASES])
def test_pattern_positive_and_negative(name: str, pos: str, neg: str) -> None:
    assert name in detect_secrets(req(pos))
    assert name not in detect_secrets(req(neg))


@pytest.mark.parametrize(
    "text",
    [
        '{"api_key": "abc123def456"}',  # JSON
        "DB_PASSWORD=hunter2hunter2",  # env
        "api-key: abc123def456",  # YAML
        "client_secret = 'zzzz1234'",
        "access_token:abcd1234",
        'x-auth-token": "abcd1234"',
    ],
)
def test_key_name_forms(text: str) -> None:
    assert detect_secrets(req(text)) == ["key_name_value"]


@pytest.mark.parametrize(
    "text", ["password: ", "token: true", "secret = ${SECRET}", "max_tokens: 99", "no key here"]
)
def test_key_name_without_value_is_clean(text: str) -> None:
    assert detect_secrets(req(text)) == []


def test_result_has_names_only() -> None:
    out = detect_secrets(req("sk-abcdefghij0123456789ABCD"))
    assert out == ["sk_key"]
    assert "abcdefghij" not in repr(out)


SECRET = "DB_PASSWORD=hunter2hunter2"


@pytest.mark.parametrize(
    "request_body",
    [
        {"messages": [{"role": "user", "content": [{"type": "text", "text": SECRET}]}]},
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {"name": "f", "arguments": '{"password": "abcd1234"}'},
                        }
                    ],
                }
            ]
        },
        {"messages": [{"role": "tool", "tool_call_id": "1", "content": SECRET}]},
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "f", "description": SECRET, "parameters": {}},
                }
            ],
        },
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "f",
                        "parameters": {
                            "properties": {"x": {"default": "sk-abcdefghij0123456789ABCD"}}
                        },
                    },
                }
            ],
        },
        {
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": {"description": SECRET}},
            },
        },
        {"messages": [{"role": "user", "content": "hi"}], "system": SECRET},
        {
            "messages": [
                {"role": "user", "content": "hi", "name": "ghp_abcdefghijklmnopqrstuvwxyz0123"}
            ]
        },
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"function": {"parameters": {"api_key": "abcd1234efgh"}}}],
        },
    ],
)
def test_every_request_location_is_scanned(request_body: dict[str, Any]) -> None:
    assert detect_secrets(request_body) != []


def test_clean_request_and_schema_keys() -> None:
    body = {
        "messages": [{"role": "user", "content": "Summarize the docs"}],
        "tools": [
            {
                "function": {
                    "name": "f",
                    "parameters": {"properties": {"password": {"type": "string"}}},
                }
            }
        ],
    }
    assert detect_secrets(body) == []
    assert list(extract_strings(body))


def test_extra_patterns() -> None:
    assert detect_secrets(req("internal-ABC-12345"), extra=[r"internal-[A-Z]{3}-\d+"]) == [
        "extra_0"
    ]
    assert detect_secrets(req("nothing"), extra=[r"internal-"]) == []


def _timed(body: dict[str, Any]) -> float:
    t = time.perf_counter()
    detect_secrets(body)
    return time.perf_counter() - t


def test_4mb_text_is_fast() -> None:
    para = "The quick brown fox jumps over the lazy dog; tokens and passes are discussed here.\n"
    text = para * (4 * 1024 * 1024 // len(para))
    assert _timed(req(text)) < 0.2
    dirty = ("api_key is noted: http://example.com/a:b token=; pass:x\n") * 80_000
    assert _timed(req(dirty[: 4 * 1024 * 1024])) < 0.2


@pytest.mark.parametrize(
    "text",
    [
        "a" * 10**6,
        "=" * 10**6,
        "eyJ" * 300_000,
        "pass" * 250_000,
        "sk-" + "a" * 10**6,
        "://u:" * 200_000,
        "-----BEGIN " * 90_000,
        "Bearer " * 140_000,
        "a:" * 500_000,
        "http" * 250_000 + "://",
        "token" + " " * 10**6 + "=",
    ],
    ids=lambda s: s[:12],
)
def test_pathological_input_is_fast(text: str) -> None:
    assert _timed(req(text)) < 0.2
