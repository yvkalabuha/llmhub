"""Secret detector for chat requests (T-369 stream R, critic high).

Scans EVERY string of a request, dict keys included (message content and content-parts,
tool_calls arguments, tool results, tools, response_format, system) and returns the NAMES of the patterns that fired,
never the matched text. Rules for the patterns: no nested quantifiers, every repetition is
bounded or anchored at a token boundary, so a hostile 1 MB string cannot stall the loop.
Callers run it in `asyncio.to_thread` (heavy pure function).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

_B = r"(?<![\w-])"  # token boundary: a run of word chars is scanned from its start only


@dataclass(frozen=True)
class _Pattern:
    name: str
    rx: re.Pattern[str]
    needles: tuple[str, ...]  # cheap prefilter on the same text the regex runs on
    # fold: regex is lowercase and runs on the lowercased string
    fold: bool = False


def _p(
    name: str, rx: str, needles: tuple[str, ...], *, flags: int = 0, fold: bool = False
) -> _Pattern:
    return _Pattern(name, re.compile(rx, flags), needles, fold)


_KEY_NAME = (  # lowercase: runs on the lowercased text
    r"(?:pass|secret|token|api[_-]?key)[\w-]{0,24}+[\"']?[ \t]{0,6}[:=][ \t]{0,6}[\"']?"
    r"(?!(?:true|false|null|none)\b)[^\s\"'$<>{}\[\](),;]{4,}"
)

PATTERNS: tuple[_Pattern, ...] = (
    _p("private_key_pem", r"-----BEGIN (?:[A-Z0-9]{1,12} ){0,3}PRIVATE KEY-----", ("-----BEGIN",)),
    _p("openssh_private_key", r"-----BEGIN OPENSSH PRIVATE KEY-----", ("-----BEGIN OPENSSH",)),
    _p("pgp_private_key", r"-----BEGIN PGP PRIVATE KEY BLOCK-----", ("-----BEGIN PGP",)),
    _p("aws_access_key", _B + r"AKIA[0-9A-Z]{16}", ("AKIA",)),
    _p("sk_key", _B + r"sk-[A-Za-z0-9_-]{20,}", ("sk-",)),
    _p(
        "stripe_key",
        _B + r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}",
        ("sk_live_", "sk_test_", "rk_live_", "rk_test_"),
    ),
    _p(
        "github_token",
        _B + r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    ),
    _p("gitlab_token", _B + r"glpat-[A-Za-z0-9_-]{20,}", ("glpat-",)),
    _p("npm_token", _B + r"npm_[A-Za-z0-9]{36}", ("npm_",)),
    _p("slack_token", _B + r"xox[abpres]-[A-Za-z0-9-]{10,}", ("xox",)),
    _p("google_api_key", _B + r"AIza[0-9A-Za-z_-]{35}", ("AIza",)),
    _p("hf_token", _B + r"hf_[A-Za-z0-9]{30,}", ("hf_",)),
    _p("runpod_key", _B + r"rpa_[A-Za-z0-9]{16,}", ("rpa_",)),
    _p("bearer_token", r"(?<![\w-])bearer[ \t]{1,5}[a-z0-9._~+/=-]{20,}", ("bearer",), fold=True),
    _p("jwt", _B + r"eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}", ("eyJ",)),
    _p(
        "uri_credentials",
        r"(?<![\w+.-])[a-z][a-z0-9+.-]{0,20}://[^/\s:@]{1,64}:[^/\s@]{1,128}@",
        ("://",),
        flags=re.IGNORECASE,
    ),
    _p("key_name_value", _KEY_NAME, ("pass", "secret", "token", "api"), fold=True),
)


def _walk(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and not isinstance(v, str):
                yield k  # a key is a string of the request too: {"sk-...": 1}
            if isinstance(v, str):
                # a native dict {"api_key": "x"} must look like text "api_key: x" to the key-name rule
                yield f"{k}: {v}"
            else:
                yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)


def extract_strings(req: dict[str, Any]) -> Iterator[str]:
    """Every string of the request: messages (content, content-parts, tool_calls arguments,
    tool results, name), tools, response_format, system and any other field."""
    yield from _walk(req)


def detect_secrets(req: dict[str, Any], extra: Sequence[str] = ()) -> list[str]:
    """Names of the patterns that fired (config order), without the matched text."""
    hits: set[str] = set()
    extra_rx = [re.compile(p) for p in extra]
    for s in extract_strings(req):
        if len(s) < 6:
            continue
        low: str | None = None
        for pat in PATTERNS:
            if pat.name in hits:
                continue
            if pat.fold:  # case-insensitive patterns are written lowercase and run on `low`
                low = s.lower() if low is None else low
                text = low
            else:
                text = s
            if not any(n in text for n in pat.needles):
                continue
            if pat.rx.search(text):
                hits.add(pat.name)
        for i, rx in enumerate(extra_rx):
            if f"extra_{i}" not in hits and rx.search(s):
                hits.add(f"extra_{i}")
    order = [p.name for p in PATTERNS] + [f"extra_{i}" for i in range(len(extra_rx))]
    return [n for n in order if n in hits]
