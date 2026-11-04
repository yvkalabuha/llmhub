"""Shared fixtures for T-370 stream E/G tests: a mock pool with a weights chain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from llmhub.cloud_manager.instance_env import InstanceSecrets
from llmhub.configuration.models import Config, PoolConfig
from tls_certs import make_self_signed

INSTANCE_SECRET = b"instance-secret-0123456789abcdef-test"  # fictitious, >= 32 bytes
WEIGHTS_SECRET = b"weights-secret-0123456789abcdef-test!"  # fictitious, >= 32 bytes
HF_SECRET = "hf_fake_token_value_for_tests"
SECRETS = InstanceSecrets(instance=INSTANCE_SECRET, weights=WEIGHTS_SECRET)
SHA_MODEL = "a" * 64
SHA_DRAFT = "b" * 64
WEIGHTS_CA = make_self_signed("127.0.0.1")
CA_PEMS = {"self_hosted": WEIGHTS_CA.cert_pem}


def _source(kind: str, ca: Path) -> dict[str, Any]:
    if kind == "self_hosted":
        return {
            "name": "self_hosted", "kind": "self_hosted",
            "base_url": "https://203.0.113.7:9443/w/{op_id}/", "ca_pem_file": str(ca),
            "min_speed_mb_s": 25, "segments": 1,
            "paths": {"model": "q/model.gguf", "draft": "q/draft.gguf"},
        }  # fmt: skip
    return {
        "name": "hf", "kind": "hf", "base_url": "https://huggingface.co/org/repo/resolve/abc/",
        "min_speed_mb_s": 0, "paths": {"model": "model.gguf", "draft": "MTP/draft.gguf"},
    }  # fmt: skip


def weights_cfg(
    data_dir: Path,
    *,
    transport: str = "proxy",
    kinds: tuple[str, ...] = ("self_hosted", "hf"),
    hf_token_env: str | None = None,
    **pool_overrides: Any,
) -> Config:
    ca = data_dir / "weights-cert.pem"
    data_dir.mkdir(parents=True, exist_ok=True)
    ca.write_text(WEIGHTS_CA.cert_pem)
    pool = PoolConfig.model_validate(
        {
            "name": "p1", "provider": "mock", "gpu": "RTX4090", "vram_gb": 24, "pp_tps": 1200,
            "tg_tps": 78, "image": "llmhub-infer:test", "disk_gb": 50, "models": ["m1"],
            "weights": "set1", "infer": {"transport": transport},
        } | pool_overrides
    )  # fmt: skip
    weights: dict[str, Any] = {
        "sources": [_source(k, ca) for k in kinds],
        "sets": {
            "set1": {
                "files": [
                    {"id": "model", "size": 300_000, "sha256": SHA_MODEL},
                    {"id": "draft", "size": 70_000, "sha256": SHA_DRAFT},
                ]
            }
        },
        "hf_token_env": hf_token_env,
    }
    return Config.model_validate({"data_dir": data_dir, "pools": {"p1": pool}, "weights": weights})


def inst_row(op_id: str = "0123456789ab", created_at: float = 1_762_000_000.0) -> dict[str, Any]:
    return {"op_id": op_id, "created_at": created_at, "id": "i-1"}
