#!/usr/bin/env python3
"""Layer-by-layer secret scan of a `docker save` tarball (T-372 E3, critic F8).

    python3 scripts/image_secret_scan.py IMAGE.tar [--report FILE]

Every layer is scanned on its own, so a secret that a later layer deletes (whiteout
`.wh.<name>`) is still found: the bytes are in the published image. The image config
(`Env`, `history[].created_by`, labels) is scanned too. Rules: private keys, GitHub / HF /
RunPod / llmhub / OpenAI-style tokens, `LLMHUB_*_SECRET=` and secret-looking file names
(`.env`, `id_rsa*`, `.netrc`, `.git-credentials`, docker/HF credential files).

Output: one line per finding `<where>: <path>: <rule>` (never the matched text), then
`verdict: clean` (exit 0) or `verdict: dirty` (exit 1); unreadable input or layer -> exit 2
(fail closed). stdlib only.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

CHUNK = 4 * 1024 * 1024
OVERLAP = 256
# (rule, literal anchor, regex matched AT the anchor, needs a word boundary before it)
RULES: tuple[tuple[str, bytes, bytes, bool], ...] = (
    # a real key has a base64 body after the header (libraries carry the bare header string
    # as a parser constant: libgnutls, libssh, libmbedcrypto, libgio)
    ("private_key", b"-----BEGIN ",
     (rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----\r?\n(?:[A-Za-z-]+:[^\r\n]*\r?\n)*(?:\r?\n)?"
      rb"[A-Za-z0-9+/]{32,}"), False),
    ("github_token", b"ghp_", rb"ghp_[A-Za-z0-9]{36}\b", True),
    ("github_pat", b"github_pat_", rb"github_pat_[A-Za-z0-9_]{60,}", True),
    ("hf_token", b"hf_", rb"hf_[A-Za-z0-9]{34,}\b", True),
    ("runpod_key", b"rpa_", rb"rpa_[A-Za-z0-9]{30,}\b", True),
    ("llmhub_key", b"lh_", rb"lh_[A-Za-z0-9_-]{40,}", True),
    ("openai_key", b"sk-", rb"sk-(?:proj-)?[A-Za-z0-9_-]{40,}", True),
    ("llmhub_secret_env", b"LLMHUB_", rb"LLMHUB_[A-Z_]*SECRET=[^\s\"']+", False),
)
_COMPILED = tuple((n, lit, re.compile(rx), wb) for n, lit, rx, wb in RULES)
_WORD = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
NAME_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dotenv", re.compile(r"(^|/)\.env(\.[A-Za-z0-9_-]+)?$")),
    ("ssh_key", re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$")),
    ("netrc", re.compile(r"(^|/)\.netrc$")),
    ("git_credentials", re.compile(r"(^|/)\.git-credentials$")),
    ("docker_auth", re.compile(r"(^|/)\.docker/config\.json$")),
    ("hf_token_file", re.compile(r"(^|/)\.cache/huggingface/token$|(^|/)\.huggingface/token$")),
    ("runpod_config", re.compile(r"(^|/)\.runpod/config\.toml$")),
)
# Known false positives of the pinned base image (path prefix -> rule). Every entry needs a
# reason and the reviewer's acceptance; a hit outside the list makes the image dirty.
ALLOW: tuple[tuple[str, str], ...] = (
    # GnuTLS FIPS 140 self-test vectors (public test keys compiled into the Ubuntu 24.04
    # library of the llama.cpp base image, layer 0); found by the T-372 scan of 0.3.0
    ("usr/lib/x86_64-linux-gnu/libgnutls.so.30", "private_key"),
)


class ScanError(Exception):
    pass


@dataclass(frozen=True)
class Finding:
    where: str
    path: str
    rule: str

    def line(self) -> str:
        return f"{self.where}: {self.path}: {self.rule}"


def scan_buffer(buf: bytes, found: set[str]) -> None:
    """Find literal anchors with `bytes.find` (fast), run the rule's regex only there."""
    for name, lit, rx, word_boundary in _COMPILED:
        if name in found:
            continue
        i = buf.find(lit)
        while i != -1:
            if not (word_boundary and i > 0 and buf[i - 1] in _WORD) and rx.match(buf, i):
                found.add(name)
                break
            i = buf.find(lit, i + 1)


def scan_stream(f: IO[bytes]) -> set[str]:
    """Rule names found in a byte stream (chunked with overlap; nothing is kept)."""
    found: set[str] = set()
    tail = b""
    while True:
        chunk = f.read(CHUNK)
        if not chunk:
            break
        buf = tail + chunk
        scan_buffer(buf, found)
        tail = buf[-OVERLAP:]
    return found


def _allowed(path: str, rule: str) -> bool:
    return any(path.startswith(p) and rule == r for p, r in ALLOW)


def scan_layer(data: IO[bytes], where: str) -> list[Finding]:
    out: list[Finding] = []
    try:
        with tarfile.open(fileobj=data, mode="r:*") as tar:
            for m in tar:
                path = m.name.removeprefix("./")
                base = path.rsplit("/", 1)[-1]
                if base.startswith(".wh."):
                    continue  # whiteout marker: the lower layer was scanned on its own
                for rule, rx in NAME_RULES:
                    if rx.search(path) and not _allowed(path, rule):
                        out.append(Finding(where, path, rule))
                if not m.isfile():
                    continue
                fh = tar.extractfile(m)
                if fh is None:
                    continue
                for rule in sorted(scan_stream(fh)):
                    if not _allowed(path, rule):
                        out.append(Finding(where, path, rule))
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ScanError(f"{where}: unreadable layer ({type(exc).__name__})") from None
    return out


def scan_config(doc: Any, where: str) -> list[Finding]:
    text = json.dumps(doc).encode()
    out = [Finding(where, "config", r) for r in sorted(scan_stream(io.BytesIO(text)))]
    cfg = doc.get("config") or {} if isinstance(doc, dict) else {}
    for env in cfg.get("Env") or []:
        name = str(env).split("=", 1)[0]
        if re.search(r"(SECRET|TOKEN|PASSWORD|API_KEY)", name) and "=" in str(env):
            value = str(env).split("=", 1)[1]
            if value:
                out.append(Finding(where, f"config.Env.{name}", "secret_env_value"))
    return out


def _manifests(tar: tarfile.TarFile) -> list[dict[str, Any]]:
    try:
        member = tar.getmember("manifest.json")
    except KeyError:
        raise ScanError("no manifest.json (not a `docker save` tarball)") from None
    fh = tar.extractfile(member)
    if fh is None:
        raise ScanError("manifest.json unreadable")
    data = json.loads(fh.read())
    if not isinstance(data, list) or not data:
        raise ScanError("manifest.json: no manifests")
    return data


def iter_findings(path: Path) -> Iterator[Finding]:
    try:
        tar = tarfile.open(path, mode="r:*")  # noqa: SIM115 - closed by the with below
    except (tarfile.TarError, OSError) as exc:
        raise ScanError(f"{path}: cannot open ({type(exc).__name__})") from None
    with tar:
        for man in _manifests(tar):
            cfg_name = str(man.get("Config", ""))
            fh = tar.extractfile(cfg_name) if cfg_name else None
            if fh is None:
                raise ScanError("image config missing")
            yield from scan_config(json.loads(fh.read()), f"config {cfg_name[-16:]}")
            layers = man.get("Layers") or []
            if not layers:
                raise ScanError("manifest without layers")
            for i, layer in enumerate(layers):
                lf = tar.extractfile(str(layer))
                if lf is None:
                    raise ScanError(f"layer {i} missing")
                yield from scan_layer(lf, f"layer {i} {str(layer)[-16:]}")  # type: ignore[arg-type]


def main(argv: list[str]) -> int:
    args = list(argv)
    report: Path | None = None
    if "--report" in args:
        i = args.index("--report")
        report = Path(args[i + 1])
        del args[i : i + 2]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    lines: list[str] = []
    try:
        findings = list(iter_findings(Path(args[0])))
    except ScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("verdict: error", file=sys.stderr)
        return 2
    for f in findings:
        lines.append(f.line())
    lines.append("verdict: " + ("dirty" if findings else "clean"))
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if report is not None:
        report.write_text(text, encoding="utf-8")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
