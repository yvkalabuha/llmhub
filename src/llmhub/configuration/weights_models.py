"""Cloud weights / pod ("infer") configuration (T-370 stream A).

Kept in its own module so that other stages add their own config modules next to it and only
touch `models.py` for one field each (additive).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_LLAMA_ARGS: tuple[str, ...] = (
    "-ngl", "999", "-fa", "on", "-c", "65536", "-np", "1", "--jinja",
    "--spec-type", "draft-mtp", "--spec-draft-n-max", "3",
)  # fmt: skip
# Set by the pod supervisor, or loading models/files, writing files, opening network paths or
# exposing extra endpoints (checked against llama-server b12063 --help). Same list in the pod
# (`llmhub_infer.supervisor`); a test keeps both in sync.
FORBIDDEN_LLAMA_FLAGS = frozenset({
    "-m", "--model", "-md", "--model-draft", "--spec-draft-model", "--host", "--port",
    "--reuse-port", "--path", "--api-prefix",
    "-mu", "--model-url", "-dr", "--docker-repo", "-hf", "-hfr", "-hff", "-hft", "-hfd", "-hfrd",
    "-mm", "--mmproj", "-mmu", "--mmproj-url", "--rpc", "--embd-gemma-default",
    "-lcs", "--lookup-cache-static", "-lcd", "--lookup-cache-dynamic",
    "--grammar-file", "-jf", "--json-schema-file", "--chat-template-file",
    "--log-file", "--slot-save-path", "--media-path", "--props", "--metrics",
    "-ag", "--agent", "--ui-config-file", "--webui-config-file", "--ui-mcp-proxy",
    "--webui-mcp-proxy", "--video-ffmpeg-dir",
})  # fmt: skip
FORBIDDEN_LLAMA_PREFIXES = (
    "--api-key", "--ssl-", "--hf-", "--spec-draft-hf", "--lora", "--control-vector",
    "--models-", "--log-prompts", "--tools", "--mcp-", "--connect", "--fim-",
)  # fmt: skip
DEFAULT_SEGMENTS = {"self_hosted": 1, "hf": 4, "r2": 1}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeightFileConfig(_Model):
    id: Literal["model", "draft"]
    size: int = Field(gt=0)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not _SHA256.match(v):
            raise ValueError("sha256 must be 64 lower-case hex chars")
        return v


class WeightSourceConfig(_Model):
    name: str = Field(min_length=1)
    kind: Literal["self_hosted", "hf", "r2"]
    base_url: str  # self_hosted: https://, may contain "{op_id}"
    paths: dict[str, str]  # file id -> relative path
    ca_pem_file: Path | None = None  # self_hosted: public cert (pin)
    min_speed_mb_s: float = Field(default=25, ge=0)
    segments: int | None = Field(default=None, ge=1, le=8)  # None -> 1 self_hosted / 4 hf

    @property
    def effective_segments(self) -> int:
        return self.segments if self.segments is not None else DEFAULT_SEGMENTS[self.kind]

    @model_validator(mode="after")
    def _check(self) -> WeightSourceConfig:
        if self.kind == "r2":
            raise ValueError("source kind r2 is not implemented")
        for fid, rel in self.paths.items():
            parts = rel.split("/")
            if not rel or rel.startswith("/") or ".." in parts or "\\" in rel:
                raise ValueError(f"paths.{fid}: must be relative without '..'")
        if self.kind == "self_hosted":
            if not self.base_url.startswith("https://"):
                raise ValueError("self_hosted base_url must be https://")
            if self.ca_pem_file is None:
                raise ValueError("self_hosted requires ca_pem_file")
        return self


class WeightSetConfig(_Model):
    files: list[WeightFileConfig]

    @model_validator(mode="after")
    def _check(self) -> WeightSetConfig:
        ids = [f.id for f in self.files]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate file id")
        if "model" not in ids:
            raise ValueError("file 'model' is required")
        return self

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


class WeightsConfig(_Model):
    sources: list[WeightSourceConfig] = Field(default_factory=list)  # order = failover chain
    sets: dict[str, WeightSetConfig] = Field(default_factory=dict)
    speed_grace_s: float = Field(default=30, gt=0)
    speed_window_s: float = Field(default=10, gt=0)
    read_timeout_s: float = Field(default=20, gt=0)
    connect_timeout_s: float = Field(default=10, gt=0)
    retries_per_source: int = Field(default=2, ge=0)
    auth_listen: str = "127.0.0.1:8096"
    # name of the variable in providers.env; None -> HF_TOKEN is never passed to the pod
    # (public repo). If set, use a fine-grained read-only token for that repository only.
    hf_token_env: str | None = None
    volume_cap_factor: float = Field(default=1.2, ge=1.0)  # served bytes per op <= f * sum(size)
    auth_state_dir: Path = Path("/var/lib/llmhub-weights-auth")
    acct_log: Path = Path("/var/log/llmhub/weights-acct.log")

    @field_validator("auth_listen")
    @classmethod
    def _loopback(cls, v: str) -> str:
        """SEC-9: the validator trusts X-Real-IP from nginx, so it must never be public."""
        host, sep, port = v.rpartition(":")
        host = host.removeprefix("[").removesuffix("]")
        if not sep or host not in ("127.0.0.1", "::1") or not port.isdigit():
            raise ValueError("auth_listen must be 127.0.0.1:<port> or [::1]:<port> (loopback)")
        if not 0 < int(port) < 65536:
            raise ValueError("auth_listen: bad port")
        return v

    @field_validator("hf_token_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        if v is not None and not _ENV_NAME.match(v):
            raise ValueError("hf_token_env must be an environment variable name")
        return v

    @model_validator(mode="after")
    def _names(self) -> WeightsConfig:
        names = [s.name for s in self.sources]
        if len(set(names)) != len(names):
            raise ValueError("duplicate source name")
        return self


class InferClientConfig(_Model):
    """Controller -> pod HTTP timeouts (stream G). All values > 0."""

    connect_s: float = Field(default=10, gt=0)
    ping_s: float = Field(default=5, gt=0)
    health_s: float = Field(default=10, gt=0)
    test_s: float = Field(default=30, gt=0)  # = timeouts.ready_test_timeout_s default
    read_s: float = Field(default=120, gt=0)  # pause between bytes of a running job
    job_cap_factor: float = Field(default=3.0, gt=0)  # cap = (in/pp + out/tg) * factor + 30 s


class InferConfig(_Model):
    transport: Literal["direct", "proxy"] = "proxy"
    port: int | None = Field(default=None, ge=1, le=65535)  # default 8443 direct / 8080 proxy
    llama_args: list[str] = Field(default_factory=lambda: list(DEFAULT_LLAMA_ARGS))
    weights_dir: str = "/models"
    client: InferClientConfig = InferClientConfig()

    @property
    def effective_port(self) -> int:
        return (
            self.port if self.port is not None else (8443 if self.transport == "direct" else 8080)
        )

    @field_validator("llama_args")
    @classmethod
    def _flags(cls, v: list[str]) -> list[str]:
        for a in v:
            flag = a.split("=", 1)[0]
            if flag in FORBIDDEN_LLAMA_FLAGS or flag.startswith(FORBIDDEN_LLAMA_PREFIXES):
                raise ValueError(f"llama_args: flag {flag} is set by the pod and forbidden")
        return v
