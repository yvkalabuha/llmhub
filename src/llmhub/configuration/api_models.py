"""API / router / local-inference configuration (T-369 stream G).

Own module (like `weights_models.py`); `models.Config` inherits `ApiSection` (one edit) so the
flat names `cfg.local_backends`, `cfg.models`, `cfg.router`, ... hold. No model names in code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from llmhub.task_queue.models import Priority

Policy = Literal["ECONOMY", "BALANCED", "QUALITY"]
AUTO_ALIAS = "auto"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalBackendConfig(_Model):
    url: str  # base URL with /v1, e.g. "http://127.0.0.1:8091/v1"
    model: str  # model name sent to llama-server
    ctx_tokens: int = Field(ge=1024)
    pp_tps: float = Field(gt=0)  # initial EWMA values
    tg_tps: float = Field(gt=0)
    timeout_s: float = 3600
    api_key_env: str | None = None  # env var holding the llama-server key; never the value
    probe: Literal["slots", "health"] = "slots"


class ModelAliasConfig(_Model):
    local: str | None = None  # key of local_backends; None = cloud only
    cloud_ctx_tokens: int = 65536
    quality_tier: int = 0
    description: str = ""


class TaskTypeConfig(_Model):
    max_tokens: int = Field(ge=1)
    think: bool | None = None  # None = leave chat_template_kwargs alone


class RouterConfig(_Model):
    default_policy: Policy = "BALANCED"
    auto_alias: str | None = None  # what "auto" expands to
    economy_local_max_eta_s: float = 3600
    balanced_local_max_eta_s: float = 600
    bytes_per_token: float = 3.5
    extra_secret_patterns: list[str] = Field(default_factory=list)


class LocalConfig(_Model):
    concurrency: int = Field(default=1, ge=1, le=1)  # one CPU server (ADR-0138 item 2)
    probe_interval_s: float = 30
    probe_timeout_s: float = 3
    ewma_alpha: float = Field(default=0.3, gt=0, le=1)
    external_busy_penalty_s: float = 120
    unavailable_after_failures: int = 3
    poll_s: float = 2.0
    max_attempts: int = 2


class ClientConfig(_Model):
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_models: list[str]  # aliases or ["*"]; [] = nothing
    max_priority: Priority = Priority.NORMAL
    default_priority: Priority = Priority.NORMAL
    default_policy: Policy = "BALANCED"
    cloud_allowed: bool = False  # data_class defaults to private


class ApiConfig(_Model):
    host: str = "127.0.0.1"
    port: int = 8095
    admin_key_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sync_wait_s: float = Field(default=480, gt=0, le=480)
    wait_poll_s: float = 0.5
    keepalive_s: float = 15
    max_body_bytes: int = 4_000_000
    retry_after_min_s: int = 5
    retry_after_max_s: int = 900


class ApiSection(_Model):
    """Fields mixed into `Config`."""

    local_backends: dict[str, LocalBackendConfig] = Field(default_factory=dict)
    models: dict[str, ModelAliasConfig] = Field(default_factory=dict)
    task_types: dict[str, TaskTypeConfig] = Field(default_factory=dict)
    router: RouterConfig = Field(default_factory=RouterConfig)
    local: LocalConfig = Field(default_factory=LocalConfig)
    clients: dict[str, ClientConfig] = Field(default_factory=dict)
    api: ApiConfig = Field(default_factory=ApiConfig)


def check_api_section(cfg: Any, pools: Mapping[str, Any]) -> None:
    """Cross-field validation; raises ValueError (called from Config's model validator)."""
    for name, m in cfg.models.items():
        if name == AUTO_ALIAS:
            raise ValueError(f"models.{name}: 'auto' is reserved")
        if m.local is not None and m.local not in cfg.local_backends:
            raise ValueError(f"models.{name}.local: unknown local backend {m.local!r}")
    for pname, pool in pools.items():  # skipped while `models` is empty (core-only configs)
        for alias in pool.models if cfg.models else ():
            if alias not in cfg.models:
                raise ValueError(f"pools.{pname}.models: alias {alias!r} not in models")
    auto = cfg.router.auto_alias
    if auto is not None and auto not in cfg.models:
        raise ValueError(f"router.auto_alias: unknown model {auto!r}")
    for pat in cfg.router.extra_secret_patterns:
        try:
            re.compile(pat)
        except re.error as exc:
            raise ValueError(f"router.extra_secret_patterns: bad regex ({exc})") from None
    admin = cfg.api.admin_key_sha256
    seen: set[str] = set()
    for cname, c in cfg.clients.items():
        for a in c.allowed_models:
            if a != "*" and a not in cfg.models:
                raise ValueError(f"clients.{cname}.allowed_models: unknown model {a!r}")
        if c.default_priority.rank < c.max_priority.rank:
            raise ValueError(f"clients.{cname}: default_priority above max_priority")
        if c.key_sha256 in seen:
            raise ValueError(f"clients.{cname}.key_sha256: duplicate key hash")
        if c.key_sha256 == admin:
            raise ValueError(f"clients.{cname}.key_sha256: equals the admin key hash")
        seen.add(c.key_sha256)
