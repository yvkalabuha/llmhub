"""T-372 E1/E2: RunPod key only in api+reaper (and only when present), s6k, the t372 config
template, render.py chain/hf options."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from llmhub.configuration.loader import load_config
from llmhub.configuration.models import IMAGE_DIGEST_RE

ROOT = Path(__file__).resolve().parent.parent
ARGS = ROOT / "deploy" / "docker" / "run-args"
SCRIPT = ROOT / "deploy" / "server" / "llmhub-deploy.sh"
CODE = SCRIPT.read_text()
T372 = ROOT / "deploy" / "config.t372.example.yaml"
NAMES = ("llmhub-api", "llmhub-reaper", "llmhub-weights-auth", "llmhub-weights-nginx")
KEY = "rpa_" + "K" * 40  # fictitious


def _func(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", CODE, re.MULTILINE | re.DOTALL)
    assert m, name
    return m.group(0)


def _render_args(tmp: Path, name: str, secret: str | None) -> list[str]:
    etc = tmp / "etc"
    (etc / "secrets").mkdir(parents=True, exist_ok=True)
    f = etc / "secrets" / "runpod"
    if secret is None:
        f.unlink(missing_ok=True)
    else:
        f.write_text(secret)
    prog = "\n".join([
        "set -euo pipefail", f"ETC={etc}", f"DIST={ROOT / 'deploy' / 'docker'}",
        "API_HOST=10.8.0.1", "API_PORT=8095", "LLAMA_GID=998", "PUBLIC_IP=203.0.113.1",
        "image_id() { echo sha256:img; }", "nginx_id() { echo sha256:ngx; }",
        "release() { echo 0.3.0; }", _func("args_of"), f'args_of "$DIST/run-args/{name}.args"',
    ])  # fmt: skip
    out = subprocess.run(["bash", "-c", prog], check=True, capture_output=True, text=True)
    return out.stdout.splitlines()


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


@pytest.mark.parametrize("name", NAMES)
def test_runpod_mount_only_api_and_reaper_when_present(tmp_path: Path, name: str) -> None:
    with_key = _render_args(tmp_path, name, KEY + "\n")
    without = _render_args(tmp_path, name, None)
    empty = _render_args(tmp_path, name, "")
    mount = "/etc/llmhub/secrets/runpod:/run/llmhub-secrets/runpod:ro"
    env = "LLMHUB_RUNPOD_API_KEY_FILE=/run/llmhub-secrets/runpod"
    ours = name in ("llmhub-api", "llmhub-reaper")
    assert (mount in _pairs(with_key, "-v")) is ours and (env in _pairs(with_key, "-e")) is ours
    for args in (without, empty):
        assert mount not in _pairs(args, "-v") and env not in _pairs(args, "-e")
    for args in (with_key, without):
        assert KEY not in "\n".join(args)
        assert not any(a.startswith("@") for a in args)


def test_runpod_args_file() -> None:
    lines = [x for x in (ARGS / "_runpod.args").read_text().splitlines()
             if x and not x.startswith("#")]  # fmt: skip
    assert lines == ["-e", "LLMHUB_RUNPOD_API_KEY_FILE=/run/llmhub-secrets/runpod", "-v",
                     "/etc/llmhub/secrets/runpod:/run/llmhub-secrets/runpod:ro"]  # fmt: skip
    for name in NAMES:
        has = "@include-if-secret runpod _runpod.args" in (ARGS / f"{name}.args").read_text()
        assert has is (name in ("llmhub-api", "llmhub-reaper"))


def test_script_checks_and_never_prints_the_key() -> None:
    assert 'stat_is "$ETC/secrets/runpod" "$UID_API:0 400"' in _func("check_s4")
    assert '"$ETC/secrets/runpod"' in _func("env_has_no_secret")
    cc = _func("config_check")
    assert "LLMHUB_RUNPOD_API_KEY_FILE=/run/s/runpod" in cc and "secrets/runpod:/run/s/runpod:ro" in cc
    for kind in ("check", "apply", "rollback"):
        assert re.search(rf"^{kind}_s6k\(\) \{{", CODE, re.MULTILINE)
    assert "s6k" in re.search(r'^STEPS="([^"]+)"', CODE, re.MULTILINE).group(1)  # type: ignore[union-attr]
    assert "shred -u" in _func("rollback_s6k") and "recreate_api_reaper" in _func("apply_s6k")
    body = _func("apply_s6k") + _func("check_s6k") + _func("rollback_s6k")
    assert not re.search(r"(cat|echo|printf)[^\n]*secrets/runpod", body.replace("head -n1", ""))
    assert "llmhub-weights-auth llmhub-weights-nginx" in _func("runpod_mounts_match")


def test_config_template_allowlist() -> None:
    f = _func("config_template")
    assert "config.example.yaml | config.t372.example.yaml" in f
    assert "LLMHUB_WEIGHTS_CHAIN" in _func("render_config")


# ------------------------------------------------------------------ E2 config template
def test_t372_config_guards() -> None:
    cfg = load_config(T372, {})
    assert cfg.providers.enabled == ["runpod"]
    rp = cfg.providers.runpod
    assert rp is not None and rp.max_live_pods == 1 and rp.cloud_type == "COMMUNITY"
    b = cfg.budget
    assert (b.hard_cap_month, b.budget_month, b.budget_day) == (4.0, 4.0, 4.0)
    assert (b.budget_session, b.budget_job, b.reserve_usd) == (0.60, 0.10, 1.0)
    (pool,) = cfg.pools.values()
    assert (pool.name, pool.provider, pool.gpu, pool.disk_gb) == ("cloud-27b", "runpod",
                                                                  "RTX24G", 40)  # fmt: skip
    assert pool.max_price_usd_h == 0.35 and pool.idle_timeout_s == 300
    assert pool.infer.transport == "proxy" and pool.weights == "local-models"
    assert IMAGE_DIGEST_RE.match(pool.image)
    assert pool.image.startswith("ghcr.io/yvkalabuha/llmhub-infer@sha256:")
    t = cfg.timeouts
    assert (t.max_lifetime_s, t.offer_wait_s, t.booting_s, t.loading_model_s) == (3600, 300,
                                                                                 600, 900)  # fmt: skip
    assert t.create_visibility_grace_s == 600 and cfg.leases.max_ttl_s == 1800
    assert cfg.retry.create_unknown_max_retries == 0 and cfg.weights.token_ttl_max_s == 1800
    cloud = {n for n, c in cfg.clients.items() if c.cloud_allowed}
    assert cloud == {"t372"}
    assert not any(c.mock_only for c in cfg.clients.values())


def test_t372_image_matches_published_digest() -> None:
    from llmhub import __version__

    digest_file = ROOT / "dist" / __version__ / "infer.digest"
    if not digest_file.exists():
        pytest.skip("P0: no dist/<ver>/infer.digest yet (GHCR push not done)")
    want = digest_file.read_text().strip()
    assert IMAGE_DIGEST_RE.match(want)
    rendered = ROOT / "dist" / __version__ / "config.t372.rendered.yaml"
    if rendered.exists():  # the coordinator's rendered copy must use the pushed digest
        assert load_config(rendered, {}).pools["cloud-27b"].image == want


# ------------------------------------------------------------------ render.py chain / hf
def _render_mod() -> Any:
    spec = importlib.util.spec_from_file_location("deploy_render_t372", ROOT / "deploy/render.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["deploy_render_t372"] = mod
    spec.loader.exec_module(mod)
    return mod


R = _render_mod()
HF_URL = "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/abc123/"


def _render(tmp: Path, *extra: str) -> tuple[int, Path]:
    models = tmp / "models"
    (models / "qwen").mkdir(parents=True, exist_ok=True)
    a = models / "qwen" / "model.gguf"
    a.write_bytes(b"m" * 4096)
    d = models / "qwen" / "draft.gguf"
    d.write_bytes(b"d" * 512)
    sums = tmp / "sha256.txt"
    sums.write_text(f"{hashlib.sha256(a.read_bytes()).hexdigest()}  {a}\n"
                    f"{hashlib.sha256(d.read_bytes()).hexdigest()}  {d}\n")  # fmt: skip
    out = tmp / "config.yaml"
    out.unlink(missing_ok=True)
    code = R.main(["config", "--template", str(T372), "--out", str(out), "--weights",
                   "--sums", str(sums), "--models-dir", str(models), "--model", "qwen/model.gguf",
                   "--draft", "qwen/draft.gguf", "--public-ip", "65.109.95.35",
                   "--ca", "/etc/llmhub/weights-pub/weights-cert.pem",
                   "--set", f"pools.cloud-27b.image=ghcr.io/yvkalabuha/llmhub-infer@sha256:{'e' * 64}",
                   *extra])  # fmt: skip
    return code, out


def test_render_t372_default_chain_needs_hf_url(tmp_path: Path) -> None:
    code, out = _render(tmp_path)
    assert code == 2 and not out.exists()  # hf base_url still has __HF_REPO__
    code, out = _render(tmp_path, "--hf-base-url", HF_URL)
    assert code == 0
    cfg = load_config(out, {})
    assert [s.name for s in cfg.weights.sources] == ["self_hosted", "hf"]
    assert cfg.weights.sources[1].base_url == HF_URL
    assert cfg.pools["cloud-27b"].image.endswith("e" * 64)


@pytest.mark.parametrize(("chain", "names"), [("hf", ["hf"]), ("self_hosted", ["self_hosted"]),
                                              ("self_hosted,hf", ["self_hosted", "hf"])])  # fmt: skip
def test_render_t372_chain(tmp_path: Path, chain: str, names: list[str]) -> None:
    extra = ["--chain", chain] + (["--hf-base-url", HF_URL] if "hf" in names else [])
    code, out = _render(tmp_path, *extra)
    assert code == 0
    assert [s.name for s in load_config(out, {}).weights.sources] == names


@pytest.mark.parametrize("extra", [["--chain", "r2"], ["--chain", "hf,hf"],
                                   ["--hf-base-url", "https://evil.example/x/"]])  # fmt: skip
def test_render_t372_bad_options(tmp_path: Path, extra: list[str]) -> None:
    code, out = _render(tmp_path, *extra)
    assert code == 2 and not out.exists()
