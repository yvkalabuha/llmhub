#!/usr/bin/env python3
"""Render server files from the templates in deploy/ (T-371). Runs in the llmhub image
(`/opt/llmhub/deploy/render.py`) or locally; no secrets pass through it (only key hashes).

    render.py config --template config.example.yaml --out config.yaml [--hashes hashes.txt]
        [--set api.host=127.0.0.1 ...] [--drop-client e2e]
        [--weights --sums /opt/models/sha256-T365.txt --models-dir /opt/models
         --model REL [--draft REL] --public-ip 65.109.95.35 --port 9443
         --ca /etc/llmhub/weights/weights-cert.pem [--set-name NAME]]
        [--chain self_hosted,hf] [--hf-base-url https://huggingface.co/ORG/REPO/resolve/REV/]
    render.py nginx --template nginx-weights.conf --out nginx.conf --var PUBLIC_IP=... ...

`--hashes`: lines `<name> <sha256>`; `admin` -> api.admin_key_sha256, other names ->
clients.<name>.key_sha256 (the client must exist in the template). `--set` values are YAML.
The rendered config is validated with `llmhub.configuration.loader.load_config` when the
package is importable. Exit 0 ok, 2 on any error (the message never carries a value of a
`--var`/`--set` that looks like a secret: only hashes are accepted anyway).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/\-\[\]]+$")
_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


class RenderError(Exception):
    pass


def parse_sha256_file(text: str, models_dir: str | None = None) -> dict[str, str]:
    """`sha256sum` output -> {relative path: sha256}. Absolute paths under `models_dir` are
    made relative; `*` (binary mode) is accepted; blank lines and `#` comments are skipped."""
    out: dict[str, str] = {}
    base = None if models_dir is None else models_dir.rstrip("/") + "/"
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not _HEX64.match(parts[0].lower()):
            raise RenderError(f"sha256 file line {n}: expected '<sha256>  <path>'")
        digest, path = parts[0].lower(), parts[1].strip().lstrip("*")
        if base is not None and path.startswith(base):
            path = path[len(base) :]
        path = path.removeprefix("./")
        if path.startswith("/") or ".." in path.split("/"):
            raise RenderError(f"sha256 file line {n}: path must be relative to the models dir")
        out[path] = digest
    return out


def render_nginx(template: str, values: Mapping[str, str]) -> str:
    """Replace `__KEY__` placeholders; values must be plain tokens (no `;`, braces, spaces or
    newlines). A placeholder left in a non-comment line is an error."""
    out = template
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise RenderError(f"bad placeholder name {key!r}")
        if not _SAFE_VALUE.match(value):
            raise RenderError(f"__{key}__: value must be a plain token")
        out = out.replace(f"__{key}__", value)
    code = "\n".join(line.split("#", 1)[0] for line in out.splitlines())
    left = sorted(set(_PLACEHOLDER.findall(code)))
    if left:
        raise RenderError("unrendered placeholders: " + ", ".join(left))
    return out


def _set_path(raw: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cur: Any = raw
    for k in keys[:-1]:
        nxt = cur.get(k) if isinstance(cur, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[keys[-1]] = value


def apply_hashes(raw: dict[str, Any], hashes: Mapping[str, str]) -> None:
    for name, digest in hashes.items():
        if not _HEX64.match(digest):
            raise RenderError(f"hash of {name!r} is not a sha256 hex")
        if name == "admin":
            raw.setdefault("api", {})["admin_key_sha256"] = digest
            continue
        clients = raw.get("clients") or {}
        if name not in clients:
            raise RenderError(f"client {name!r} is not in the template")
        clients[name]["key_sha256"] = digest


def parse_hashes(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not _NAME.match(parts[0]) or not _HEX64.match(parts[1]):
            raise RenderError(f"hashes line {n}: expected '<name> <sha256>'")
        out[parts[0]] = parts[1]
    return out


def fill_weight_sets(
    raw: dict[str, Any],
    sums: Mapping[str, str],
    sizes: Mapping[str, int],
    *,
    model: str,
    draft: str | None,
    public_ip: str,
    port: int,
    ca_pem_file: str,
    set_name: str = "local-models",
    source_name: str = "self_hosted",
) -> None:
    """weights.sources (one self_hosted source) and weights.sets[set_name] from the sums file.
    Pools are not touched (Mock pods download nothing; T-372 attaches sets to real pools)."""
    files: list[dict[str, Any]] = []
    paths: dict[str, str] = {}
    for fid, rel in (("model", model), ("draft", draft)):
        if rel is None:
            continue
        if rel not in sums:
            raise RenderError(f"{fid} {rel!r} is not in the sha256 file")
        size = sizes.get(rel)
        if not size or size <= 0:
            raise RenderError(f"{fid} {rel!r}: size unknown (models dir not readable?)")
        files.append({"id": fid, "size": int(size), "sha256": sums[rel]})
        paths[fid] = rel
    w = raw.setdefault("weights", {})
    # T-372: other template sources (e.g. hf) stay behind self_hosted (the failover chain)
    others = [s for s in w.get("sources") or [] if isinstance(s, dict)
              and s.get("kind") != "self_hosted"]  # fmt: skip
    w["sources"] = [{
        "name": source_name, "kind": "self_hosted",
        "base_url": f"https://{public_ip}:{port}/w/{{op_id}}/", "ca_pem_file": ca_pem_file,
        "segments": 1, "paths": paths,
    }, *others]  # fmt: skip
    w.setdefault("sets", {})[set_name] = {"files": files}


_HF_URL = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/resolve/[A-Za-z0-9._-]+/$")


def apply_chain(raw: dict[str, Any], chain: Sequence[str] | None, hf_base_url: str | None) -> None:
    """T-372: `--hf-base-url` sets the hf source; `--chain a,b` keeps these sources in this
    order. A kept source whose base_url still holds a `__PLACEHOLDER__` is an error."""
    w = raw.get("weights") or {}
    sources = [s for s in w.get("sources") or [] if isinstance(s, dict)]
    if hf_base_url is not None:
        if not _HF_URL.match(hf_base_url):
            raise RenderError("--hf-base-url must be https://huggingface.co/<org>/<repo>/resolve/<rev>/")
        hf = [s for s in sources if s.get("kind") == "hf"]
        if not hf:
            raise RenderError("--hf-base-url: the template has no hf source")
        for s in hf:
            s["base_url"] = hf_base_url
    if chain is not None:
        by_name = {str(s.get("name")): s for s in sources}
        missing = [n for n in chain if n not in by_name]
        if missing or len(set(chain)) != len(chain) or not chain:
            raise RenderError(f"--chain: unknown or repeated source names {missing or list(chain)}")
        sources = [by_name[n] for n in chain]
        w["sources"] = sources
    for s in sources:
        if _PLACEHOLDER.search(str(s.get("base_url", ""))):
            raise RenderError(f"weights source {s.get('name')!r}: base_url still has a placeholder")


def render_config(
    template: str,
    *,
    hashes: Mapping[str, str] | None = None,
    sets: Sequence[tuple[str, Any]] = (),
    drop_clients: Sequence[str] = (),
) -> dict[str, Any]:
    raw = yaml.safe_load(template)
    if not isinstance(raw, dict):
        raise RenderError("config template must be a mapping")
    clients = raw.get("clients") or {}
    for name in drop_clients:  # e.g. `e2e` at the end of S10 (F7); its hash is ignored
        clients.pop(name, None)
    if hashes:
        apply_hashes(raw, {n: h for n, h in hashes.items() if n not in drop_clients})
    for dotted, value in sets:
        _set_path(raw, dotted, value)
    return raw


def _validate(out: Path) -> None:
    try:
        from llmhub.configuration.loader import ConfigError, load_config
    except ImportError:  # pragma: no cover - rendering outside the image/venv
        return
    try:
        load_config(out, env={})
    except ConfigError as exc:
        raise RenderError(f"rendered config is invalid: {exc}") from None


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _cmd_config(a: argparse.Namespace) -> None:
    template = Path(a.template).read_text(encoding="utf-8")
    hashes = parse_hashes(Path(a.hashes).read_text(encoding="utf-8")) if a.hashes else None
    sets: list[tuple[str, Any]] = []
    for item in a.set or []:
        key, sep, value = item.partition("=")
        if not sep or not re.fullmatch(r"[a-z_][a-z0-9_.-]*", key):
            raise RenderError(f"--set expects key.path=value, got {key!r}")
        sets.append((key, yaml.safe_load(value)))
    raw = render_config(template, hashes=hashes, sets=sets, drop_clients=a.drop_client or [])
    if a.weights:
        if not (a.sums and a.model and a.public_ip and a.ca):
            raise RenderError("--weights needs --sums, --model, --public-ip and --ca")
        sums = parse_sha256_file(Path(a.sums).read_text(encoding="utf-8"), a.models_dir)
        sizes: dict[str, int] = {}
        for rel in (a.model, a.draft):
            if rel is not None:
                p = Path(a.models_dir) / rel
                if p.is_symlink() or not p.is_file():
                    raise RenderError(f"{rel!r}: not a regular file under the models dir")
                sizes[rel] = p.stat().st_size
        fill_weight_sets(raw, sums, sizes, model=a.model, draft=a.draft,
                         public_ip=a.public_ip, port=a.port, ca_pem_file=a.ca,
                         set_name=a.set_name)  # fmt: skip
    chain = [c for c in a.chain.split(",") if c] if a.chain else None
    apply_chain(raw, chain, a.hf_base_url)  # also refuses a kept __PLACEHOLDER__ base_url
    out = Path(a.out)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("# rendered by deploy/render.py - edit the template, not this file\n"
                   + yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")  # fmt: skip
    try:
        _validate(tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)


def _cmd_nginx(a: argparse.Namespace) -> None:
    values: dict[str, str] = {}
    for item in a.var or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise RenderError("--var expects KEY=VALUE")
        values[key] = value
    text = render_nginx(Path(a.template).read_text(encoding="utf-8"), values)
    _write(Path(a.out), text)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="render.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("config")
    c.add_argument("--template", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--hashes")
    c.add_argument("--set", action="append")
    c.add_argument("--drop-client", action="append")
    c.add_argument("--weights", action="store_true")
    c.add_argument("--sums")
    c.add_argument("--models-dir", default="/opt/models")
    c.add_argument("--model")
    c.add_argument("--draft")
    c.add_argument("--public-ip")
    c.add_argument("--port", type=int, default=9443)
    c.add_argument("--ca")
    c.add_argument("--set-name", default="local-models")
    c.add_argument("--chain", default=None, help="T-372: weights sources to keep, in order (a,b)")
    c.add_argument("--hf-base-url", default=None, help="T-372: base_url of the hf source")
    n = sub.add_parser("nginx")
    n.add_argument("--template", required=True)
    n.add_argument("--out", required=True)
    n.add_argument("--var", action="append")
    a = ap.parse_args(argv)
    try:
        _cmd_config(a) if a.cmd == "config" else _cmd_nginx(a)
    except (RenderError, OSError, yaml.YAMLError) as exc:
        print(f"render error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
