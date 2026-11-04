"""Typed configuration (pydantic v2, extra=forbid). The label secret is NOT part of it."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llmhub.configuration.api_models import ApiSection, check_api_section
from llmhub.configuration.weights_models import InferConfig, WeightsConfig


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueueClassConfig(_Model):
    minimum_batch_size: int = Field(ge=1)
    maximum_batch_wait_s: float = Field(ge=0)
    maximum_queue_wait_s: float = Field(ge=0)


class QueueConfig(_Model):
    urgent: QueueClassConfig = QueueClassConfig(
        minimum_batch_size=1, maximum_batch_wait_s=0, maximum_queue_wait_s=0
    )
    normal: QueueClassConfig = QueueClassConfig(
        minimum_batch_size=1, maximum_batch_wait_s=0, maximum_queue_wait_s=900
    )
    low: QueueClassConfig = QueueClassConfig(
        minimum_batch_size=5, maximum_batch_wait_s=1800, maximum_queue_wait_s=5400
    )
    deadline_margin_s: float = 60
    implicit_dedup_window_s: float = 900
    idem_ttl_s: float = 604800
    deadline_includes_work: bool = True  # False only in simulator-parity mode


class TimeoutsConfig(_Model):
    provisioning_s: float = 600
    booting_s: float = 600
    loading_model_s: float = 1200
    stop_confirm_s: float = 180
    leak_after_s: float = 900
    # parity tests bypass this validator via model_construct / override
    max_lifetime_s: int = Field(default=10800, ge=600, le=10800)
    ready_test_timeout_s: float = 30
    model_test_retries: int = 3
    model_test_window_s: float = 60
    worker_heartbeat_s: float = 30
    worker_stale_s: float = 180
    network_loss_s: float = 180
    offer_wait_s: float = 600
    create_visibility_grace_s: float = 120


class LeaseConfig(_Model):
    default_ttl_s: float = 600
    min_ttl_s: float = 30
    max_ttl_s: float = 3600
    hold_max_ttl_s: float = Field(default=28800, le=28800)
    hold_may_exceed_session: bool = False


class PoolConfig(_Model):
    name: str
    provider: str
    gpu: str
    vram_gb: int
    max_price_usd_h: float = 0.50
    idle_timeout_s: float = Field(default=300, ge=0, le=900)
    max_concurrency: int = Field(default=1, ge=1)
    pp_tps: float
    tg_tps: float
    cold_start_s_default: float = 540
    image: str
    disk_gb: int
    models: list[str] = []
    subbudget: str | None = None
    weights: str | None = None  # key of Config.weights.sets (T-370)
    infer: InferConfig = InferConfig()


class BudgetConfig(_Model):
    hard_cap_month: float = 50
    budget_month: float = 45
    budget_day: float = 3
    budget_session: float = 1
    budget_job: float = 0.25
    subbudgets: dict[str, float] = {"exception": 10}
    warn_levels: tuple[float, float, float] = (0.5, 0.8, 0.95)
    pace_factor: float = 1.25
    stage_economy: float = 0.8
    stage_urgent_only: float = 0.95
    reserve_usd: float = 1.0
    min_balance_hours: float = 1.0
    divergence_abs: float = 0.05
    divergence_rel: float = 0.05
    timezone: str = "UTC"


class RetryConfig(_Model):
    get_retries: int = 3
    get_backoff_s: float = 1.0
    create_max_attempts: int = 2
    job_max_attempts: int = 2
    breaker_errors: int = 3
    breaker_window_s: float = 600
    breaker_open_s: float = 900
    host_blacklist_s: float = 86400
    destroy_backoff_s: float = 5.0
    destroy_backoff_max_s: float = 120.0
    call_timeout_s: float = 20


class ProvidersConfig(_Model):
    enabled: list[str] = ["mock"]


class DbConfig(_Model):
    busy_timeout_ms: int = Field(default=300, ge=250, le=500)
    reaper_busy_timeout_ms: int = 5000


class Config(ApiSection):
    data_dir: Path
    pools: dict[str, PoolConfig]
    queue: QueueConfig = QueueConfig()
    timeouts: TimeoutsConfig = TimeoutsConfig()
    leases: LeaseConfig = LeaseConfig()
    budget: BudgetConfig = BudgetConfig()
    retry: RetryConfig = RetryConfig()
    providers: ProvidersConfig = ProvidersConfig()
    db: DbConfig = DbConfig()
    task_type_max_tokens: dict[str, int] = Field(default_factory=lambda: {"default": 8000})
    weights: WeightsConfig = WeightsConfig()

    @model_validator(mode="after")
    def _check_weights(self) -> Config:
        w = self.weights
        for name, pool in self.pools.items():
            if pool.weights is None:
                continue
            wset = w.sets.get(pool.weights)
            if wset is None:
                raise ValueError(f"pools.{name}.weights: unknown weight set {pool.weights!r}")
            if not w.sources:
                raise ValueError(f"pools.{name}.weights: weights.sources is empty")
            for src in w.sources:
                for f in wset.files:
                    if f.id not in src.paths:
                        raise ValueError(f"weights.sources.{src.name}: no path for file {f.id!r}")
        check_api_section(self, self.pools)
        return self
