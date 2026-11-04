"""T-371 E1: deploy/render.py - sha256 file, nginx rendering, config with hashes and weights."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from llmhub.configuration.loader import load_config
from llmhub.weights.auth_app import tables_from_config

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "deploy/config.example.yaml"
NGINX = ROOT / "deploy/nginx-weights.conf"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("deploy_render", ROOT / "deploy/render.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["deploy_render"] = mod
    spec.loader.exec_module(mod)
    return mod


r = _load()
H = [f"{i:x}" * 64 for i in range(1, 6)]
VARS = {
    "PUBLIC_IP": "65.109.95.35", "PORT": "9443", "RATE": "80m", "SEGMENTS": "1", "MAX_DL": "1",
    "MODELS": "/opt/models", "AUTH": "127.0.0.1:8096", "LOGDIR": "/var/log/llmhub",
    "CERT": "/etc/llmhub/weights/weights-cert.pem", "KEY": "/etc/llmhub/weights/weights-key.pem",
    "PID": "/tmp/nginx.pid", "TMP": "/tmp",
}  # fmt: skip


def test_parse_sha256_file() -> None:
    text = (f"{H[0]}  /opt/models/qwen/a.gguf\n# comment\n\n{H[1]} *qwen/b.gguf\n"
            f"{H[2]}  ./c/d.gguf\n")  # fmt: skip
    assert r.parse_sha256_file(text, "/opt/models") == {
        "qwen/a.gguf": H[0], "qwen/b.gguf": H[1], "c/d.gguf": H[2],
    }  # fmt: skip


@pytest.mark.parametrize("bad", ["nothex  a.gguf", f"{H[0]}", f"{H[0]}  ../x.gguf",
                                 f"{H[0]}  /etc/passwd"])  # fmt: skip
def test_parse_sha256_file_rejects(bad: str) -> None:
    with pytest.raises(r.RenderError):
        r.parse_sha256_file(bad, "/opt/models")


def test_render_nginx_full() -> None:
    out = r.render_nginx(NGINX.read_text(), VARS)
    code = "\n".join(line.split("#", 1)[0] for line in out.splitlines())
    assert not re.search(r"__[A-Z_]+__", code)
    assert "listen 65.109.95.35:9443 ssl;" in out
    assert "limit_rate 80m;" in out and "limit_conn w_all 1;" in out
    assert "limit_conn w_op 1;" in out
    assert "directio 4m;" in out and "output_buffers 2 1m;" in out


def test_render_nginx_missing_placeholder() -> None:
    vars_ = {k: v for k, v in VARS.items() if k != "MAX_DL"}
    with pytest.raises(r.RenderError, match="__MAX_DL__"):
        r.render_nginx(NGINX.read_text(), vars_)


@pytest.mark.parametrize("value", ["80m; return 200", "a b", "x\ny", "{ }", ""])
def test_render_nginx_rejects_injection(value: str) -> None:
    with pytest.raises(r.RenderError):
        r.render_nginx(NGINX.read_text(), {**VARS, "RATE": value})


def _hashes(tmp: Path) -> Path:
    p = tmp / "hashes.txt"
    names = ["admin", "voicebot", "gamedev", "agents", "e2e"]
    p.write_text("".join(f"{n} {h}\n" for n, h in zip(names, H, strict=True)))
    return p


def test_render_config_with_hashes_and_sets(tmp_path: Path) -> None:
    out = tmp_path / "config.yaml"
    code = r.main(["config", "--template", str(TEMPLATE), "--out", str(out),
                   "--hashes", str(_hashes(tmp_path)), "--set", "api.host=127.0.0.1",
                   "--set", "local_backends.qwen-9b.url=http://127.0.0.1:18092/v1"])  # fmt: skip
    assert code == 0
    cfg = load_config(out)
    assert cfg.api.admin_key_sha256 == H[0]
    assert cfg.clients["e2e"].key_sha256 == H[4] and cfg.clients["e2e"].mock_only
    assert cfg.api.host == "127.0.0.1" and cfg.api.docs is False
    assert cfg.local_backends["qwen-9b"].url == "http://127.0.0.1:18092/v1"


def test_render_config_unknown_client_hash(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text(f"stranger {H[0]}\n")
    out = tmp_path / "config.yaml"
    assert r.main(["config", "--template", str(TEMPLATE), "--out", str(out),
                   "--hashes", str(p)]) == 2  # fmt: skip
    assert not out.exists()


def test_invalid_result_is_not_written(tmp_path: Path) -> None:
    out = tmp_path / "config.yaml"
    assert r.main(["config", "--template", str(TEMPLATE), "--out", str(out),
                   "--set", "api.host=0.0.0.0"]) == 2  # fmt: skip
    assert not out.exists() and not list(tmp_path.glob("*.tmp"))


def test_render_config_weights(tmp_path: Path) -> None:
    models = tmp_path / "models"
    (models / "qwen").mkdir(parents=True)
    a = models / "qwen" / "model.gguf"
    a.write_bytes(b"m" * 4096)
    d = models / "qwen" / "draft.gguf"
    d.write_bytes(b"d" * 512)
    sums = tmp_path / "sha256.txt"
    sums.write_text(f"{hashlib.sha256(a.read_bytes()).hexdigest()}  {a}\n"
                    f"{hashlib.sha256(d.read_bytes()).hexdigest()}  {d}\n")  # fmt: skip
    out = tmp_path / "config.yaml"
    code = r.main(["config", "--template", str(TEMPLATE), "--out", str(out), "--weights",
                   "--sums", str(sums), "--models-dir", str(models), "--model", "qwen/model.gguf",
                   "--draft", "qwen/draft.gguf", "--public-ip", "65.109.95.35",
                   "--ca", "/etc/llmhub/weights/weights-cert.pem"])  # fmt: skip
    assert code == 0
    cfg = load_config(out)
    allowed, sizes = tables_from_config(cfg)
    assert allowed == {"qwen/model.gguf", "qwen/draft.gguf"}
    assert sizes["qwen/model.gguf"] == (4096, 4608)
    src = cfg.weights.sources[0]
    assert src.base_url == "https://65.109.95.35:9443/w/{op_id}/"
    assert cfg.weights.min_mem_available_mib == 8192
    assert all(p.weights is None for p in cfg.pools.values())


def test_render_config_weights_missing_file(tmp_path: Path) -> None:
    sums = tmp_path / "s.txt"
    sums.write_text(f"{H[0]}  qwen/model.gguf\n")
    out = tmp_path / "c.yaml"
    assert r.main(["config", "--template", str(TEMPLATE), "--out", str(out), "--weights",
                   "--sums", str(sums), "--models-dir", str(tmp_path), "--model",
                   "qwen/model.gguf", "--public-ip", "1.2.3.4", "--ca", "/x.pem"]) == 2  # fmt: skip


def test_template_is_plain_yaml_mapping() -> None:
    assert isinstance(yaml.safe_load(TEMPLATE.read_text()), dict)


def test_drop_client(tmp_path: Path) -> None:
    out = tmp_path / "config.yaml"
    assert r.main(["config", "--template", str(TEMPLATE), "--out", str(out),
                   "--hashes", str(_hashes(tmp_path)), "--drop-client", "e2e"]) == 0  # fmt: skip
    cfg = load_config(out)
    assert "e2e" not in cfg.clients and cfg.clients["voicebot"].key_sha256 == H[1]
