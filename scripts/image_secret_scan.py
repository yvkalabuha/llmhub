#!/usr/bin/env python3
"""Layer-by-layer secret scan of a `docker save` tarball (T-372 E3, critic F8).

    python3 scripts/image_secret_scan.py IMAGE.tar [--report FILE]

Every layer is scanned on its own, so a secret that a later layer deletes (whiteout
`.wh.<name>`) is still found: the bytes are in the published image. The image config
(`Env`, `history[].created_by`, labels) is scanned too. Rules: private keys, GitHub / HF /
RunPod / llmhub / OpenAI-style tokens, `LLMHUB_*_SECRET=` and secret-looking file names
(`.env`, `id_rsa*`, `.netrc`, `.git-credentials`, docker/HF credential files).

Output: one line per finding `<where>: <path>: <rule>` (never the matched text), one line
`allowed: <where>: <path>: <rule> (<reason>)` per allowlisted hit (REV-S6: an allowlist entry
is an EXACT path in a layer with a listed digest; the digest of that layer blob is verified),
then `verdict: clean` (exit 0) or `verdict: dirty` (exit 1); unreadable input or layer, or a
layer whose content does not match its digest -> exit 2 (fail closed). stdlib only.
"""

from __future__ import annotations

import hashlib
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
# Ubuntu 24.04 base layer of the pinned llama.cpp image (layer 0 of llmhub-infer 0.3.x): the
# `docker save` blob digest (gzip) and its uncompressed diff_id (the same content).
_UBUNTU_BASE_LAYER = frozenset({
    "sha256:5a7813e071bfadf18aaa6ca8318be4824a9b6297b3240f2cc84c1db6f4113040",
    "sha256:4b7c01ed0534d4f9be9cf97d068da1598c6c20b26cb6134fad066defdb6d541d",
})


@dataclass(frozen=True)
class Allow:
    path: str  # exact path inside the layer
    rule: str
    layers: frozenset[str]  # digests of the layers where it may appear
    reason: str


# Known false positives of the pinned base image. Every entry needs a reason and the
# reviewer's acceptance (customer decision 2026-11-04 for the GnuTLS one, on condition REV-S6:
# exact path + layer digest, hits written to the report); anything else makes the image dirty.
ALLOW: tuple[Allow, ...] = (
    Allow("usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1", "private_key", _UBUNTU_BASE_LAYER,
          "GnuTLS FIPS 140 self-test vectors (public test keys of libgnutls30t64)"),
)
_BLOB = re.compile(r"^blobs/sha256/([0-9a-f]{64})$")


class ScanError(Exception):
    pass


@dataclass(frozen=True)
class Finding:
    where: str
    path: str
    rule: str
    allowed_by: str = ""  # reason of the allowlist entry; "" = a real finding

    def line(self) -> str:
        if self.allowed_by:
            return f"allowed: {self.where}: {self.path}: {self.rule} ({self.allowed_by})"
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


def _allowed(path: str, rule: str, layer: str | None) -> Allow | None:
    """The entry that exempts this exact path + rule in this layer (digest), or None."""
    if layer is None:
        return None
    return next((a for a in ALLOW if a.path == path and a.rule == rule and layer in a.layers),
                None)  # fmt: skip


def layer_digest(name: str) -> str | None:
    """`blobs/sha256/<hex>` (OCI layout of `docker save`) -> `sha256:<hex>`; else None (no
    allowlist applies to a layer without a content address)."""
    m = _BLOB.match(name)
    return f"sha256:{m.group(1)}" if m else None


def _finding(where: str, path: str, rule: str, layer: str | None) -> Finding:
    a = _allowed(path, rule, layer)
    return Finding(where, path, rule, a.reason if a is not None else "")


def scan_layer(data: IO[bytes], where: str, layer: str | None = None) -> list[Finding]:
    out: list[Finding] = []
    try:
        with tarfile.open(fileobj=data, mode="r:*") as tar:
            for m in tar:
                path = m.name.removeprefix("./")
                base = path.rsplit("/", 1)[-1]
                if base.startswith(".wh."):
                    continue  # whiteout marker: the lower layer was scanned on its own
                for rule, rx in NAME_RULES:
                    if rx.search(path):
                        out.append(_finding(where, path, rule, layer))
                if not m.isfile():
                    continue
                fh = tar.extractfile(m)
                if fh is None:
                    continue
                for rule in sorted(scan_stream(fh)):
                    out.append(_finding(where, path, rule, layer))
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
                digest = layer_digest(str(layer))
                found = scan_layer(lf, f"layer {i} {str(layer)[-16:]}", digest)  # type: ignore[arg-type]
                if any(f.allowed_by for f in found):  # the exemption trusts the digest: verify
                    _verify_digest(tar, str(layer), str(digest), i)
                yield from found


def _verify_digest(tar: tarfile.TarFile, name: str, digest: str, i: int) -> None:
    fh = tar.extractfile(name)
    if fh is None:
        raise ScanError(f"layer {i} missing")
    h = hashlib.sha256()
    for chunk in iter(lambda: fh.read(CHUNK), b""):
        h.update(chunk)
    if "sha256:" + h.hexdigest() != digest:
        raise ScanError(f"layer {i}: content does not match its digest")


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
    dirty = [f for f in findings if not f.allowed_by]
    for f in findings:
        lines.append(f.line())
    lines.append("verdict: " + ("dirty" if dirty else "clean"))
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if report is not None:
        report.write_text(text, encoding="utf-8")
    return 1 if dirty else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
