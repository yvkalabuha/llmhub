from pathlib import Path

import pytest
from pydantic import ValidationError

from llmhub.configuration.loader import ConfigError, load_config, load_label_secret
from llmhub.configuration.models import (
    BudgetConfig,
    Config,
    DbConfig,
    LeaseConfig,
    PoolConfig,
    QueueConfig,
    TimeoutsConfig,
)

POOL = {
    "name": "p1", "provider": "mock", "gpu": "RTX4090", "vram_gb": 24, "pp_tps": 1200,
    "tg_tps": 78, "image": "llmhub-infer:test", "disk_gb": 50, "models": ["m1"],
}  # fmt: skip


def test_defaults() -> None:
    q = QueueConfig()
    assert (q.urgent.minimum_batch_size, q.urgent.maximum_batch_wait_s) == (1, 0)
    assert q.normal.maximum_queue_wait_s == 900
    assert (q.low.minimum_batch_size, q.low.maximum_batch_wait_s, q.low.maximum_queue_wait_s) == (
        5, 1800, 5400,
    )  # fmt: skip
    assert q.deadline_margin_s == 60 and q.implicit_dedup_window_s == 900
    assert q.idem_ttl_s == 604800 and q.deadline_includes_work is True
    assert PoolConfig(**POOL).idle_timeout_s == 300
    assert PoolConfig(**POOL).max_price_usd_h == 0.50
    b = BudgetConfig()
    assert (b.hard_cap_month, b.budget_month, b.budget_day) == (50, 45, 3)
    assert (b.budget_session, b.budget_job, b.subbudgets) == (1, 0.25, {"exception": 10})
    assert b.warn_levels == (0.5, 0.8, 0.95) and b.timezone == "UTC"
    t = TimeoutsConfig()
    assert t.provisioning_s == 600 and t.leak_after_s == 900 and t.max_lifetime_s == 10800
    assert LeaseConfig().default_ttl_s == 600 and LeaseConfig().hold_max_ttl_s == 28800
    assert LeaseConfig().hold_may_exceed_session is False
    d = DbConfig()
    assert d.busy_timeout_ms == 300 and d.reaper_busy_timeout_ms == 5000


@pytest.mark.parametrize("idle", [901, -1])
def test_idle_out_of_range(idle: int) -> None:
    with pytest.raises(ValidationError):
        PoolConfig(**{**POOL, "idle_timeout_s": idle})


def test_hold_too_long() -> None:
    with pytest.raises(ValidationError):
        LeaseConfig(hold_max_ttl_s=28801)


def test_busy_timeout_range() -> None:
    with pytest.raises(ValidationError):
        DbConfig(busy_timeout_ms=100)
    with pytest.raises(ValidationError):
        DbConfig(busy_timeout_ms=600)


def test_max_lifetime_range_and_parity_override() -> None:
    with pytest.raises(ValidationError):
        TimeoutsConfig(max_lifetime_s=86400)
    assert TimeoutsConfig.model_construct(max_lifetime_s=86400).max_lifetime_s == 86400


def test_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        QueueConfig(bogus=1)  # type: ignore[call-arg]


def test_load_config_ok(tmp_path: Path) -> None:
    f = tmp_path / "c.yaml"
    f.write_text(
        f"data_dir: {tmp_path}\npools:\n  p1:\n"
        + "".join(f"    {k}: {v!r}\n" for k, v in POOL.items())
    )
    cfg = load_config(f, {})
    assert isinstance(cfg, Config)
    assert cfg.pools["p1"].gpu == "RTX4090"
    assert cfg.providers.enabled == ["mock"]
    assert cfg.task_type_max_tokens == {"default": 8000}


def test_load_config_error_has_path(tmp_path: Path) -> None:
    f = tmp_path / "c.yaml"
    f.write_text(f"data_dir: {tmp_path}\nleases:\n  hold_max_ttl_s: 90000\n")
    with pytest.raises(ConfigError, match=r"leases\.hold_max_ttl_s"):
        load_config(f, {})


def test_secret_too_short() -> None:
    with pytest.raises(ConfigError) as ei:
        load_label_secret({"LLMHUB_LABEL_SECRET": "short"})
    assert "short" not in str(ei.value)
    with pytest.raises(ConfigError):
        load_label_secret({})


def test_secret_ok_and_repr_hides_it(tmp_path: Path) -> None:
    secret = "x" * 40
    assert load_label_secret({"LLMHUB_LABEL_SECRET": secret}) == secret.encode()
    cfg = Config(data_dir=tmp_path, pools={})
    assert secret not in repr(cfg)
