"""Result checks run by the MCP server (T-373 plan §3.4).

Syntax follows the `extllm_checks` pilot of the agents workspace (code is not shared). Reasons carry numbers and names
only, never fragments of the checked text.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

MAX_PATTERN = 300
SCRIPTS = ("latin", "cyrillic", "han")
_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


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

    def __call__(self, text: str, finish_reason: str | None) -> CheckResult:
        return self.fn(text, finish_reason)


def _unfence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


def _load_json(text: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(_unfence(text))
    except ValueError:
        return False, None


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


def _json_schema(schema: dict[str, Any]) -> Check:
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError:
        raise CheckSpecError("json-schema: the schema is not a valid JSON Schema") from None
    validator = Draft202012Validator(schema)

    def run(text: str, _f: str | None) -> CheckResult:
        ok, data = _load_json(text)
        if not ok:
            return CheckResult("json-schema", False, "not_json")
        err = best_match(validator.iter_errors(data))
        if err is None:
            return CheckResult("json-schema", True)
        path = "/" + "/".join(str(p) for p in err.absolute_path)
        return CheckResult("json-schema", False, f"{path}: {err.validator}")

    return Check("json-schema", run)


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
    ok, data = _load_json(text)
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
        rx = re.compile(pattern, re.MULTILINE)
    except re.error:
        raise CheckSpecError(f"{name}: pattern does not compile") from None

    def run(text: str, _f: str | None) -> CheckResult:
        found = rx.search(text) is not None
        if negate:
            return CheckResult(name, not found, "found" if found else "")
        return CheckResult(name, found, "" if found else "not found")

    return Check(name, run)


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
    return [c(text, finish_reason) for c in checks]


async def run_checks_async(
    text: str, finish_reason: str | None, checks: Sequence[Check], *, timeout_s: float = 5.0
) -> list[CheckResult]:
    """Each check in a worker thread with a timeout (ReDoS guard, plan R5). A timed-out thread
    keeps running in the background; acceptable for a per-agent stdio process."""
    out: list[CheckResult] = []
    for c in checks:
        try:
            out.append(await asyncio.wait_for(asyncio.to_thread(c, text, finish_reason), timeout_s))
        except TimeoutError:
            out.append(CheckResult(c.name, False, "timeout"))
    return out
