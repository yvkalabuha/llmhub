import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from llmhub.configuration import (
    Config,
    ConfigError,
    InferClientConfig,
    InferConfig,
    WeightsConfig,
    WeightSourceConfig,
    load_config,
    load_instance_secret,
    load_weights_ca,
    load_weights_secret,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "config.cloud.example.yaml"
SHA = "a" * 64


def raw() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load(EXAMPLE.read_text()))


def build(r: dict[str, Any]) -> Config:
    for n, p in r["pools"].items():
        p.setdefault("name", n)
    return Config.model_validate(r)


def test_example_loads() -> None:
    cfg = load_config(EXAMPLE, {})
    w = cfg.weights
    assert [s.name for s in w.sources] == ["self_hosted", "hf"]
    assert w.sources[0].effective_segments == 1 and w.sources[1].effective_segments == 4
    assert w.hf_token_env is None
    assert cfg.pools["cloud-27b"].weights == "qwen38-27b-q4"
    assert w.sets["qwen38-27b-q4"].total_size == 1470000000 + 17680000000


def test_old_config_without_weights_valid() -> None:
    cfg = Config.model_validate({"data_dir": "/x", "pools": {}})
    assert cfg.weights == WeightsConfig() and cfg.weights.hf_token_env is None
    assert cfg.weights.volume_cap_factor == 1.2 and cfg.weights.auth_listen == "127.0.0.1:8096"


def test_default_segments() -> None:
    kw: dict[str, Any] = {"name": "x", "base_url": "https://h/", "paths": {}}
    assert WeightSourceConfig(kind="hf", **kw).effective_segments == 4
    assert WeightSourceConfig(kind="hf", segments=2, **kw).effective_segments == 2
    s = WeightSourceConfig(kind="self_hosted", ca_pem_file=Path("c.pem"), **kw)
    assert s.effective_segments == 1


def mutate(fn: Any) -> dict[str, Any]:
    r = raw()
    fn(r)
    return r


BAD = {
    "r2": lambda r: r["weights"]["sources"][1].update(kind="r2"),
    "http": lambda r: r["weights"]["sources"][0].update(base_url="http://h/"),
    "no_ca": lambda r: r["weights"]["sources"][0].pop("ca_pem_file"),
    "dotdot": lambda r: r["weights"]["sources"][1]["paths"].update(model="a/../b.gguf"),
    "abs": lambda r: r["weights"]["sources"][1]["paths"].update(model="/b.gguf"),
    "unknown_set": lambda r: r["pools"]["cloud-27b"].update(weights="nope"),
    "missing_path": lambda r: r["weights"]["sources"][1]["paths"].pop("draft"),
    "empty_chain": lambda r: r["weights"].update(sources=[]),
    "bad_sha": lambda r: r["weights"]["sets"]["qwen38-27b-q4"]["files"][0].update(sha256="xyz"),
    "no_model": lambda r: r["weights"]["sets"]["qwen38-27b-q4"]["files"].pop(),
    "dup_file": lambda r: r["weights"]["sets"]["qwen38-27b-q4"]["files"].append(
        {"id": "draft", "size": 1, "sha256": SHA}
    ),
    "segments0": lambda r: r["weights"]["sources"][1].update(segments=0),
    "segments9": lambda r: r["weights"]["sources"][1].update(segments=9),
    "dup_source": lambda r: r["weights"]["sources"][1].update(name="self_hosted"),
    "bad_env": lambda r: r["weights"].update(hf_token_env="A B"),
    "extra": lambda r: r["weights"].update(bogus=1),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_invalid(case: str) -> None:
    with pytest.raises(ValidationError):
        build(mutate(BAD[case]))


@pytest.mark.parametrize(
    "flag", ["--host", "-m", "--api-key", "--ssl-key-file", "--port=1", "-md", "--model"]
)
def test_forbidden_llama_flag(flag: str) -> None:
    with pytest.raises(ValidationError):
        InferConfig(llama_args=["-ngl", "9", flag, "x"])


def test_infer_defaults() -> None:
    i = InferConfig()
    assert i.transport == "proxy" and i.effective_port == 8080 and i.weights_dir == "/models"
    assert InferConfig(transport="direct").effective_port == 8443
    assert InferConfig(transport="direct", port=9000).effective_port == 9000
    assert "--spec-type" in i.llama_args and i.llama_args[:2] == ["-ngl", "999"]


def test_client_defaults_and_positive() -> None:
    c = InferClientConfig()
    assert (c.connect_s, c.ping_s, c.health_s, c.test_s, c.read_s, c.job_cap_factor) == (
        10, 5, 10, 30, 120, 3.0,
    )  # fmt: skip
    for f in ("connect_s", "ping_s", "health_s", "test_s", "read_s", "job_cap_factor"):
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                InferClientConfig(**{f: bad})


def test_hf_token_env_accepted() -> None:
    r = raw()
    r["weights"]["hf_token_env"] = "HF_READ_TOKEN"
    assert build(r).weights.hf_token_env == "HF_READ_TOKEN"


@pytest.mark.parametrize(
    ("fn", "name"),
    [
        (load_instance_secret, "LLMHUB_INSTANCE_SECRET"),
        (load_weights_secret, "LLMHUB_WEIGHTS_SECRET"),
    ],
)
def test_secrets(fn: Any, name: str) -> None:
    assert fn({name: "x" * 32}) == b"x" * 32
    with pytest.raises(ConfigError, match=name):
        fn({})
    with pytest.raises(ConfigError) as ei:
        fn({name: "short-secret-value"})
    assert "short-secret-value" not in str(ei.value)


def test_load_weights_ca(tmp_path: Path) -> None:
    pem = tmp_path / "c.pem"
    pem.write_text("PEMDATA")
    r = raw()
    r["weights"]["sources"][0]["ca_pem_file"] = str(pem)
    cfg = build(r)
    assert load_weights_ca(cfg) == {"self_hosted": "PEMDATA"}
    pem.unlink()
    with pytest.raises(ConfigError, match="self_hosted"):
        load_weights_ca(cfg)
