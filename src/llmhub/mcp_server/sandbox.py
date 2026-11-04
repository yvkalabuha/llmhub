"""Regex-bearing checks in a separate, killable process (T-373 review SEC-1).

The `re` engine (also behind JSON Schema `pattern`/`patternProperties`) holds the GIL, so a
catastrophic pattern in a thread freezes the whole stdio server. The parent therefore runs such a
check in a one-shot child (`python -I -m llmhub.mcp_server.sandbox`) and kills it on timeout.
Protocol: one JSON line on stdin, one JSON line on the child's stdout (a pipe to the parent, never
the MCP channel). The child also arms `SIGALRM` (default action: terminate) so it dies on its own
if the parent disappears. Pure evaluation helpers live here so the child imports nothing heavy
(jsonschema is imported only for schema checks).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import sys
from typing import Any, cast

MAX_REGEX_TEXT = 200_000  # characters; longer text fails a regex check without searching
MAX_REASON_SEGMENT = 40
MAX_REASON_PATH = 200
_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)
_MODULE = "llmhub.mcp_server.sandbox"

Outcome = tuple[bool, str]


def unfence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


def load_json(text: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(unfence(text))
    except (ValueError, RecursionError):
        return False, None


def regex_outcome(pattern: str, negate: bool, text: str) -> Outcome:
    if len(text) > MAX_REGEX_TEXT:
        return False, f"text longer than {MAX_REGEX_TEXT} characters"
    found = re.compile(pattern, re.MULTILINE).search(text) is not None
    if negate:
        return not found, "found" if found else ""
    return found, "" if found else "not found"


def _reason_path(parts: Any) -> str:
    segs = [str(p)[:MAX_REASON_SEGMENT] for p in parts]
    return ("/" + "/".join(segs))[:MAX_REASON_PATH]


def schema_outcome(schema: dict[str, Any], text: str) -> Outcome:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match

    ok, data = load_json(text)
    if not ok:
        return False, "not_json"
    try:
        err = best_match(Draft202012Validator(schema).iter_errors(data))
    except Exception:  # noqa: BLE001 - unresolvable $ref, RecursionError, ...: never raise
        return False, "schema_error"
    if err is None:
        return True, ""
    return False, f"{_reason_path(err.absolute_path)}: {str(err.validator)[:MAX_REASON_SEGMENT]}"


def evaluate(spec: dict[str, Any], text: str) -> Outcome:
    if spec.get("op") == "regex":
        return regex_outcome(str(spec["pattern"]), bool(spec["negate"]), text)
    if spec.get("op") == "json-schema":
        return schema_outcome(cast(dict[str, Any], spec["schema"]), text)
    return False, "sandbox_error"


# --- parent side ---------------------------------------------------------------------------
def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("LLMHUB_", "PYTHON"))}


async def run_isolated(spec: dict[str, Any], text: str, timeout_s: float) -> Outcome | None:
    """Evaluate `spec` in a child process; `None` = timed out (child killed)."""
    if timeout_s <= 0:
        return None
    payload = json.dumps({"spec": spec, "text": text, "alarm_s": math.ceil(timeout_s) + 1})
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", _MODULE,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=_child_env(),
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(payload.encode("utf-8") + b"\n"),
                                        timeout_s)  # fmt: skip
    except TimeoutError:
        return None
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
    try:
        data = json.loads(out)
        return bool(data["ok"]), str(data["reason"])
    except (ValueError, KeyError, TypeError):
        return False, "sandbox_error"


# --- child side ----------------------------------------------------------------------------
def main() -> int:
    req = json.loads(sys.stdin.buffer.readline())
    signal.alarm(int(req.get("alarm_s") or 10))  # default SIGALRM action terminates the child
    try:
        ok, reason = evaluate(req["spec"], req["text"])
    except Exception:  # noqa: BLE001
        ok, reason = False, "sandbox_error"
    os.write(1, json.dumps({"ok": ok, "reason": reason}).encode("utf-8") + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
