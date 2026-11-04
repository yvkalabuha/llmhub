"""Result checks run by the MCP server (T-373 plan §3.4).

Syntax follows the `extllm_checks` pilot of the agents workspace (code is not shared). Reasons
carry numbers, names and length-capped schema paths only. Regex-bearing checks (`regex`,
`no-regex`, `json-schema` with `pattern`/`patternProperties`) run in a killable child process
(`sandbox.py`, review SEC-1); the rest run inline (linear in the text size).
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import unquote

import jsonschema
from jsonschema import Draft202012Validator

from llmhub.mcp_server.sandbox import load_json, regex_outcome, run_isolated, schema_outcome

MAX_PATTERN = 300
DEFAULT_TIMEOUT_S = 5.0
SCRIPTS = ("latin", "cyrillic", "han")
_REGEX_KEYS = frozenset({"pattern", "patternProperties"})


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    reason: str = ""


class CheckSpecError(ValueError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    fn: Callable[[str, str | None], CheckResult]
    isolated: dict[str, Any] | None = None  # JSON spec for the sandbox process (regex-bearing)

    def __call__(self, text: str, finish_reason: str | None) -> CheckResult:
        return self.fn(text, finish_reason)


def _nonneg_int(arg: str, name: str) -> int:
    if not arg.isdigit():
        raise CheckSpecError(f"{name}: expected a non-negative integer")
    return int(arg)


# --- individual checks ---------------------------------------------------------------------
def _max_bytes(limit: int) -> Check:
    def run(text: str, _f: str | None) -> CheckResult:
        n = len(text.encode("utf-8"))
        return CheckResult("max-bytes", n <= limit, "" if n <= limit else f"{n} > {limit}")

    return Check("max-bytes", run)


def _not_truncated() -> Check:
    def run(_t: str, finish: str | None) -> CheckResult:
        ok = finish != "length"
        return CheckResult("not-truncated", ok, "" if ok else "finish_reason=length")

    return Check("not-truncated", run)


def _walk(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """All mappings of the schema tree, iteratively (no RecursionError on deep schemas)."""
    out: list[dict[str, Any]] = []
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            d = cast(dict[str, Any], node)
            out.append(d)
            stack.extend(d.values())
        elif isinstance(node, list):
            stack.extend(cast(list[Any], node))
    return out


def _pointer(schema: dict[str, Any], fragment: str) -> bool:
    node: Any = schema
    for raw in fragment.split("/")[1:]:
        part = unquote(raw).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = cast(dict[str, Any], node)[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(cast(list[Any], node)):
            node = cast(list[Any], node)[int(part)]
        else:
            return False
    return isinstance(node, dict | bool)


def _check_refs(schema: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
    """Only local references that resolve inside this schema (review C2-F2): an unresolvable
    `$ref` would otherwise surface only after the LLM answered."""
    anchors = {n.get(k) for n in nodes for k in ("$anchor", "$dynamicAnchor")}
    if any("$id" in n for n in nodes if n is not schema):
        raise CheckSpecError("json-schema: nested $id is not supported")
    for n in nodes:
        for key in ("$ref", "$dynamicRef"):
            ref = n.get(key)
            if not isinstance(ref, str):
                continue
            if not ref.startswith("#"):
                raise CheckSpecError("json-schema: only local references (#...) are allowed")
            frag = ref[1:]
            ok = _pointer(schema, frag) if frag == "" or frag.startswith("/") else frag in anchors
            if not ok:
                raise CheckSpecError("json-schema: a reference does not resolve")


def _json_schema(schema: dict[str, Any]) -> Check:
    try:
        Draft202012Validator.check_schema(schema)
        nodes = _walk(schema)
    except jsonschema.SchemaError:
        raise CheckSpecError("json-schema: the schema is not a valid JSON Schema") from None
    except RecursionError:
        raise CheckSpecError("json-schema: the schema is nested too deeply") from None
    _check_refs(schema, nodes)
    regex_bearing = any(_REGEX_KEYS & n.keys() for n in nodes)

    def run(text: str, _f: str | None) -> CheckResult:
        ok, reason = schema_outcome(schema, text)
        return CheckResult("json-schema", ok, reason)

    spec = {"op": "json-schema", "schema": schema} if regex_bearing else None
    return Check("json-schema", run, spec)


def _sections(headings: list[str]) -> Check:
    def run(text: str, _f: str | None) -> CheckResult:
        lines = [ln.strip() for ln in text.splitlines()]
        pos = 0
        for n, h in enumerate(headings, 1):
            try:
                pos = lines.index(h, pos) + 1
            except ValueError:
                return CheckResult("sections", False, f"missing or out of order: {n}")
        return CheckResult("sections", True)

    return Check("sections", run)


def _count_items(text: str) -> int:
    ok, data = load_json(text)
    if ok and isinstance(data, list):
        return len(data)  # pyright: ignore[reportUnknownArgumentType]
    if ok and isinstance(data, dict):
        arrays = [v for v in data.values() if isinstance(v, list)]  # pyright: ignore[reportUnknownVariableType]
        if len(arrays) == 1:
            return len(arrays[0])  # pyright: ignore[reportUnknownArgumentType]
    return sum(1 for ln in text.splitlines() if ln.strip())


def _item_count(expected: int) -> Check:
    def run(text: str, _f: str | None) -> CheckResult:
        n = _count_items(text)
        ok = n == expected
        return CheckResult("item-count", ok, "" if ok else f"{n} items, expected {expected}")

    return Check("item-count", run)


def _is_script(ch: str, script: str) -> bool:
    name = unicodedata.name(ch, "")
    if script == "han":
        return name.startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY"))
    return name.startswith(script.upper())


def _script(script: str, ratio: float) -> Check:
    name = f"script:{script}"

    def run(text: str, _f: str | None) -> CheckResult:
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return CheckResult(name, False, "no letters")
        got = sum(1 for c in letters if _is_script(c, script)) / len(letters)
        ok = got >= ratio
        return CheckResult(name, ok, "" if ok else f"ratio {got:.2f} < {ratio:g}")

    return Check(name, run)


def _regex(pattern: str, *, negate: bool) -> Check:
    name = "no-regex" if negate else "regex"
    if not pattern:
        raise CheckSpecError(f"{name}: empty pattern")
    if len(pattern) > MAX_PATTERN:
        raise CheckSpecError(f"{name}: pattern longer than {MAX_PATTERN} characters")
    try:
        re.compile(pattern, re.MULTILINE)
    except (re.error, RecursionError, OverflowError):
        raise CheckSpecError(f"{name}: pattern does not compile") from None

    def run(text: str, _f: str | None) -> CheckResult:
        ok, reason = regex_outcome(pattern, negate, text)
        return CheckResult(name, ok, reason)

    return Check(name, run, {"op": "regex", "pattern": pattern, "negate": negate})


# --- parsing -------------------------------------------------------------------------------
def parse_check(spec: str) -> Check:
    name, sep, arg = spec.partition(":")
    if name == "not-truncated" and not sep:
        return _not_truncated()
    if name == "max-bytes" and sep:
        return _max_bytes(_nonneg_int(arg, name))
    if name == "item-count" and sep:
        return _item_count(_nonneg_int(arg, name))
    if name == "sections" and sep:
        headings = [h.strip() for h in arg.split(",") if h.strip()]
        if not headings:
            raise CheckSpecError("sections: no headings")
        return _sections(headings)
    if name == "script" and sep:
        script, sep2, raw = arg.partition(":")
        if script not in SCRIPTS or not sep2:
            raise CheckSpecError("script: expected script:<latin|cyrillic|han>:<ratio>")
        try:
            ratio = float(raw)
        except ValueError:
            raise CheckSpecError("script: ratio must be a number in 0..1") from None
        if not 0 <= ratio <= 1:
            raise CheckSpecError("script: ratio must be a number in 0..1")
        return _script(script, ratio)
    if name in ("regex", "no-regex") and sep:
        return _regex(arg, negate=name == "no-regex")
    raise CheckSpecError("unknown check specification")


def build_checks(
    specs: Sequence[str], schema: dict[str, Any] | None, *, kind_defaults: Sequence[str]
) -> list[Check]:
    """`not-truncated` first (always), then `json-schema` (with `schema`), kind defaults, own."""
    out: list[Check] = [_not_truncated()]
    seen: set[str] = {"not-truncated"}
    if schema is not None:
        out.append(_json_schema(schema))
    for spec in [*kind_defaults, *specs]:
        spec = spec.strip()
        if spec in seen:
            continue
        seen.add(spec)
        if spec == "json-schema":
            if schema is None:
                raise CheckSpecError("json-schema: needs `schema`")
            continue
        out.append(parse_check(spec))
    return out


def run_checks(text: str, finish_reason: str | None, checks: Sequence[Check]) -> list[CheckResult]:
    """In-process, no time limit: for trusted text only (tests, kinds validation)."""
    return [c(text, finish_reason) for c in checks]


async def run_checks_async(
    text: str,
    finish_reason: str | None,
    checks: Sequence[Check],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[CheckResult]:
    """`timeout_s` is one budget for all checks of the call (review C2-F4). Regex-bearing checks
    run in a child process that is killed when the budget is over (SEC-1), so the event loop
    stays free; the others (pure Python, linear) run in a worker thread. A check past the budget fails with reason `timeout`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    out: list[CheckResult] = []
    for c in checks:
        left = deadline - loop.time()
        if left <= 0:
            out.append(CheckResult(c.name, False, "timeout"))
            continue
        if c.isolated is None:
            try:
                out.append(await asyncio.wait_for(asyncio.to_thread(c, text, finish_reason), left))
            except TimeoutError:
                out.append(CheckResult(c.name, False, "timeout"))
            continue
        res = await run_isolated(c.isolated, text, left)
        out.append(CheckResult(c.name, False, "timeout") if res is None
                   else CheckResult(c.name, res[0], res[1]))  # fmt: skip
    return out
