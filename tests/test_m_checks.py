"""T-373 B1: result checks (own code; syntax like extllm_checks)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from llmhub.mcp_server import checks as checks_mod
from llmhub.mcp_server.checks import (
    Check,
    CheckSpecError,
    build_checks,
    parse_check,
    run_checks,
    run_checks_async,
)

SECRET_TEXT = "SENSITIVE-PAYLOAD-42"


def one(spec: str, text: str, finish: str | None = "stop") -> Any:
    return parse_check(spec)(text, finish)


def test_max_bytes() -> None:
    assert one("max-bytes:5", "abcde").ok
    r = one("max-bytes:5", "абв")  # 6 bytes
    assert not r.ok and r.reason == "6 > 5" and r.name == "max-bytes"


@pytest.mark.parametrize("spec", ["max-bytes:", "max-bytes:x", "max-bytes:-1", "max-bytes"])
def test_max_bytes_bad_spec(spec: str) -> None:
    with pytest.raises(CheckSpecError):
        parse_check(spec)


def test_not_truncated() -> None:
    assert one("not-truncated", "x", "stop").ok
    assert one("not-truncated", "x", None).ok
    r = one("not-truncated", "x", "length")
    assert not r.ok and r.reason == "finish_reason=length"


def test_not_truncated_always_added_first() -> None:
    names = [c.name for c in build_checks(["max-bytes:9"], None, kind_defaults=[])]
    assert names == ["not-truncated", "max-bytes"]
    assert [c.name for c in build_checks([], None, kind_defaults=[])] == ["not-truncated"]
    dup = build_checks(["not-truncated", "max-bytes:9"], None, kind_defaults=["max-bytes:9"])
    assert [c.name for c in dup] == ["not-truncated", "max-bytes"]


SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
    "required": ["items"],
}


def _schema_check(text: str) -> Any:
    checks = build_checks([], SCHEMA, kind_defaults=[])
    assert [c.name for c in checks] == ["not-truncated", "json-schema"]
    return run_checks(text, "stop", checks)[1]


def test_json_schema_ok_plain_and_fenced() -> None:
    assert _schema_check('{"items": [1, 2]}').ok
    assert _schema_check('```json\n{"items": [1, 2]}\n```').ok
    assert _schema_check('```\n{"items": []}\n```\n').ok


def test_json_schema_fail_reason_is_path_not_value() -> None:
    r = _schema_check(json.dumps({"items": [1, 2, 3, SECRET_TEXT]}))
    assert not r.ok and r.reason == "/items/3: type" and SECRET_TEXT not in r.reason


def test_json_schema_missing_required() -> None:
    r = _schema_check(json.dumps({"other": SECRET_TEXT}))
    assert not r.ok and r.reason == "/: required" and SECRET_TEXT not in r.reason


def test_not_json() -> None:
    r = _schema_check(f"Sure! {SECRET_TEXT}")
    assert not r.ok and r.reason == "not_json"


def test_explicit_json_schema_without_schema_is_spec_error() -> None:
    with pytest.raises(CheckSpecError):
        build_checks(["json-schema"], None, kind_defaults=[])
    assert len(build_checks(["json-schema"], SCHEMA, kind_defaults=[])) == 2


def test_bad_schema_is_spec_error() -> None:
    with pytest.raises(CheckSpecError):
        build_checks([], {"type": 5}, kind_defaults=[])


def test_sections_in_order() -> None:
    text = "intro\n## A\nbody\n  ## B  \nmore"
    assert one("sections:## A,## B", text).ok
    r = one("sections:## B,## A", text)
    assert not r.ok and r.reason == "missing or out of order: 2" and SECRET_TEXT not in r.reason
    assert not one("sections:## C", text).ok
    assert not one("sections:A", "xx A xx").ok  # must be a whole line


def test_sections_bad_spec() -> None:
    with pytest.raises(CheckSpecError):
        parse_check("sections:")


def test_item_count_array_object_lines() -> None:
    assert one("item-count:3", "[1, 2, 3]").ok
    assert one("item-count:2", '```json\n{"labels": ["a", "b"], "n": 2}\n```').ok
    assert one("item-count:2", "one\n\n two \n").ok
    r = one("item-count:3", '{"a": [1], "b": [2]}')  # two arrays -> counted as lines
    assert not r.ok and r.reason == "1 items, expected 3"
    assert not one("item-count:2", "[1]").ok


@pytest.mark.parametrize("spec", ["item-count:", "item-count:a", "item-count:-2"])
def test_item_count_bad_spec(spec: str) -> None:
    with pytest.raises(CheckSpecError):
        parse_check(spec)


def test_script_ratio() -> None:
    assert one("script:cyrillic:0.9", "Привет, мир! 123").ok
    assert one("script:latin:0.5", "Hello мир").ok
    assert one("script:han:0.5", "你好世界 ok").ok
    r = one("script:han:0.5", "hello 你")
    assert not r.ok and r.reason == "ratio 0.17 < 0.5" and r.name == "script:han"
    assert one("script:latin:0.1", "123 !!!").reason == "no letters"


@pytest.mark.parametrize("spec", [
    "script:greek:0.5", "script:latin", "script:latin:x", "script:latin:1.5", "script:latin:-0.1",
])  # fmt: skip
def test_script_bad_spec(spec: str) -> None:
    with pytest.raises(CheckSpecError):
        parse_check(spec)


def test_regex_and_no_regex() -> None:
    assert one(r"regex:^\d+$", "abc\n42\n").ok  # MULTILINE
    r = one(r"regex:^\d+$", f"{SECRET_TEXT} abc")
    assert not r.ok and r.reason == "not found" and SECRET_TEXT not in r.reason
    assert one("no-regex:TODO", "done").ok
    r = one("no-regex:TODO", "a TODO b")
    assert not r.ok and r.reason == "found"
    assert one("regex:a:b", "xa:by").ok  # the pattern keeps its colons


def test_regex_bad_specs() -> None:
    with pytest.raises(CheckSpecError):
        parse_check("regex:(")
    with pytest.raises(CheckSpecError):
        parse_check("regex:" + "a" * 301)
    parse_check("regex:" + "a" * 300)
    with pytest.raises(CheckSpecError):
        parse_check("regex:")


@pytest.mark.parametrize("spec", ["", "nope", "numbers:/x", "max-bytes 5"])
def test_unknown_check(spec: str) -> None:
    with pytest.raises(CheckSpecError):
        parse_check(spec)


def test_spec_error_message_has_no_pattern() -> None:
    with pytest.raises(CheckSpecError) as exc:
        parse_check("regex:(" + SECRET_TEXT)
    assert SECRET_TEXT not in str(exc.value)


def test_kind_defaults_come_first() -> None:
    names = [c.name for c in build_checks(["regex:x"], None, kind_defaults=["max-bytes:10"])]
    assert names == ["not-truncated", "max-bytes", "regex"]


async def test_async_timeout_marks_check_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(text: str, finish: str | None) -> Any:
        time.sleep(0.3)
        return checks_mod.CheckResult("slow", True)

    res = await run_checks_async("x", "stop", [Check("slow", slow)], timeout_s=0.05)
    assert res[0].name == "slow" and not res[0].ok and res[0].reason == "timeout"


async def test_async_matches_sync() -> None:
    cs = build_checks(["max-bytes:3"], None, kind_defaults=[])
    assert await run_checks_async("abcd", "stop", cs) == run_checks("abcd", "stop", cs)
