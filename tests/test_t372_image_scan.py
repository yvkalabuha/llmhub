"""T-372 E3: layer-by-layer secret scan of a `docker save` tarball (synthetic images)."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from t372_scripts import load

ROOT = Path(__file__).resolve().parent.parent
scan = load("image_secret_scan")

HF = "hf_" + "A1b2C3d4E5" * 4  # fictitious
PEM = ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "b3BlbnNzaC1rZXktdjEAAAAA" * 3
       + "\n-----END OPENSSH PRIVATE KEY-----\n")  # fictitious


def _layer(files: Mapping[str, str | None], *, gz: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as t:
        for name, text in files.items():
            data = b"" if text is None else text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _image(tmp: Path, layers: list[bytes], config: dict[str, object] | None = None) -> Path:
    cfg = config or {"config": {"Env": ["PATH=/usr/bin"]}, "history": [{"created_by": "RUN x"}]}
    out = tmp / "image.tar"
    with tarfile.open(out, "w") as t:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))

        add("blobs/sha256/cfg0000000000000000", json.dumps(cfg).encode())
        names = []
        for i, data in enumerate(layers):
            n = f"blobs/sha256/layer{i:016d}"
            add(n, data)
            names.append(n)
        add("manifest.json", json.dumps([{"Config": "blobs/sha256/cfg0000000000000000",
                                          "Layers": names}]).encode())  # fmt: skip
    return out


def _run(path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = scan.main([str(path)])
    return code, capsys.readouterr().out


def test_clean_image(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    img = _image(tmp_path, [_layer({"usr/bin/app": "binary"}), _layer({"etc/x": "hello"}, gz=True)])
    code, out = _run(img, capsys)
    assert code == 0 and out.strip() == "verdict: clean"


def test_secret_in_lower_layer_removed_in_upper_is_dirty(tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """critic F8: `RUN rm` / whiteout does not remove the bytes from the published image."""
    lower = _layer({"root/.cache/huggingface/token": HF, "opt/app/cfg.txt": f"token={HF}"})
    upper = _layer({"root/.cache/huggingface/.wh.token": None, "opt/app/.wh.cfg.txt": None})
    code, out = _run(_image(tmp_path, [lower, upper]), capsys)
    assert code == 1 and out.splitlines()[-1] == "verdict: dirty"
    assert "root/.cache/huggingface/token: hf_token_file" in out
    assert "opt/app/cfg.txt: hf_token" in out
    assert HF not in out  # only paths and rule names are printed


@pytest.mark.parametrize(
    ("files", "rule"),
    [
        ({"home/u/.ssh/id_ed25519": PEM}, "private_key"),
        ({"app/.env": "X=1"}, "dotenv"),
        ({"app/run.sh": "export LLMHUB_WEIGHTS_SECRET=abcdef"}, "llmhub_secret_env"),
        ({"app/k": "lh_" + "x" * 43}, "llmhub_key"),
        ({"app/k": "rpa_" + "Z" * 40}, "runpod_key"),
        ({"app/k": "ghp_" + "a" * 36}, "github_token"),
        ({"root/.docker/config.json": "{}"}, "docker_auth"),
    ],
)
def test_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str], files: dict[str, str],
               rule: str) -> None:  # fmt: skip
    code, out = _run(_image(tmp_path, [_layer(files)]), capsys)
    assert code == 1 and f": {rule}" in out


def test_bare_pem_header_constant_is_not_a_key(tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """libgnutls/libssh carry "-----BEGIN ... PRIVATE KEY-----" as a parser constant."""
    lib = "\x7fELF..-----BEGIN RSA PRIVATE KEY-----\x00-----BEGIN PRIVATE KEY-----\x00..."
    code, out = _run(_image(tmp_path, [_layer({"usr/lib/libssh.so": lib})]), capsys)
    assert code == 0 and out.strip() == "verdict: clean"
    enc = ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00\n\n"
           + "MIIEowIBAAKCAQEA" * 4 + "\n")
    code, out = _run(_image(tmp_path, [_layer({"etc/k.pem": enc})]), capsys)
    assert code == 1 and "private_key" in out


def test_config_history_and_env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = {"config": {"Env": ["HF_TOKEN=abc123"]},
           "history": [{"created_by": f"RUN echo {HF} > /dev/null"}]}  # fmt: skip
    code, out = _run(_image(tmp_path, [_layer({"a": "b"})], cfg), capsys)
    assert code == 1 and "config.Env.HF_TOKEN: secret_env_value" in out and "config: hf_token" in out
    assert "abc123" not in out


def test_secret_across_chunk_boundary(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                      monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr(scan, "CHUNK", 64)
    code, out = _run(_image(tmp_path, [_layer({"f": "x" * 50 + " " + HF})]), capsys)
    assert code == 1 and "hf_token" in out


def test_unreadable_is_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.tar"
    bad.write_bytes(b"not a tar")
    assert scan.main([str(bad)]) == 2
    img = _image(tmp_path, [b"garbage-layer"])
    assert scan.main([str(img)]) == 2


def test_publish_script_order() -> None:
    text = (ROOT / "deploy" / "docker" / "publish-infer.sh").read_text()
    i_scan = text.index("image_secret_scan.py")
    i_login = text.index("login ghcr.io")
    i_push = text.index("docker push")
    i_logout = text.rindex("logout ghcr.io")
    assert i_scan < i_login < i_push < i_logout
    assert "--password-stdin" in text and "DOCKER_CONFIG" in text
    assert "infer.digest" in text and "verdict: clean" in text
    for bad in ("set -x", "echo \"$TOKEN", "cat \"$TOKEN"):
        assert bad not in text


def test_allowlist_is_exact_and_small(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert scan.ALLOW == (("usr/lib/x86_64-linux-gnu/libgnutls.so.30", "private_key"),)
    ok = _image(tmp_path, [_layer({"usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1": PEM})])
    assert _run(ok, capsys)[0] == 0
    other = _image(tmp_path, [_layer({"usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1": HF,
                                      "usr/lib/x86_64-linux-gnu/libother.so": PEM})])  # fmt: skip
    code, out = _run(other, capsys)
    assert code == 1 and "libgnutls.so.30.37.1: hf_token" in out and "libother.so" in out
